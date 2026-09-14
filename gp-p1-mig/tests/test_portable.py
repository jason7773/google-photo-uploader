from pathlib import Path
import json
import pytest

from gp_p1_mig import settings
from gp_p1_mig.db import transaction
from gp_p1_mig.workflow import cmd_import_verify, cmd_purge, preview_purge, MigError


@pytest.fixture
def batch(workspace):
    root, db = workspace
    original = root / 'data/work/extracted/photo.jpg'
    sidecar = original.with_suffix('.json')
    patched = root / 'data/work/patched/photo.jpg'
    folder = root / 'data/work/batches/test/files'
    folder.mkdir(parents=True)
    for path in [original, sidecar, patched, folder / 'photo.jpg']:
        path.write_text('keep me')
    with transaction(db) as c:
        c.execute("INSERT INTO batches(batch_id,status,local_batch_path) VALUES ('test','PUSHED',?)", (str(folder.parent),))
        c.execute("INSERT INTO media_items(content_id,original_name,ext,extracted_path,sidecar_path,patched_path,batch_id) VALUES ('one','photo.jpg','jpg',?,?,?,'test')", (str(original),str(sidecar),str(patched)))
        c.execute("INSERT INTO batch_items(batch_id,content_id,file_name,file_size) VALUES ('test','one','photo.jpg',7)")
    return root, db, original, sidecar, patched, folder


def verification_csv(root, body):
    p = root / 'verify.csv'
    p.write_text('batch_id,content_id,result\n' + body)
    return p


@pytest.mark.parametrize('body', ['test,one,\n', 'other,one,ok\n', 'test,other,ok\n', 'test,one,ok\ntest,one,ok\n', 'test,one,failed\n'])
def test_reject_invalid_verification(batch, body):
    root, db, *_ = batch
    with pytest.raises(MigError):
        cmd_import_verify(db,'test',verification_csv(root,body))
    with transaction(db) as c:
        assert c.execute("SELECT status FROM batches").fetchone()[0] == 'PUSHED'


def test_reject_unpushed_and_partial_verification(batch):
    root, db, *_ = batch
    csv = verification_csv(root,'test,one,ok\n')
    with transaction(db) as c:
        c.execute("UPDATE batches SET status='CREATED'")
    with pytest.raises(MigError):
        cmd_import_verify(db,'test',csv)
    with transaction(db) as c:
        c.execute("UPDATE batches SET status='PUSHED'")
        c.execute("INSERT INTO media_items(content_id,original_name,ext,extracted_path) VALUES ('two','two.jpg','jpg','unused')")
        c.execute("INSERT INTO batch_items(batch_id,content_id,file_name,file_size) VALUES ('test','two','two.jpg',1)")
    with pytest.raises(MigError):
        cmd_import_verify(db,'test',csv)


def test_cleanup_preserves_originals_and_archives_copies(batch):
    root, db, original, sidecar, patched, folder = batch
    cmd_import_verify(db,'test',verification_csv(root,'test,one,ok\n'))
    assert preview_purge(root,db,'test')['count'] == 2
    result = cmd_purge(root,db,'test')
    assert original.read_text() == sidecar.read_text() == 'keep me'
    assert not patched.exists() and not folder.exists()
    manifest = json.loads((Path(result['trash_dir'])/'manifest.json').read_text())
    assert all(Path(item['destination']).exists() for item in manifest['files'])
    with transaction(db) as c:
        assert c.execute('SELECT COUNT(*) FROM media_items').fetchone()[0] == 1
        assert c.execute('SELECT status FROM batches').fetchone()[0] == 'PURGED'


def test_cleanup_outside_root_rejected_before_any_move(batch, tmp_path):
    root, db, original, sidecar, patched, folder = batch
    outside = tmp_path / 'outside.jpg'
    outside.write_text('untouched')
    with transaction(db) as c:
        c.execute("UPDATE batches SET status='VERIFIED'")
        c.execute('UPDATE media_items SET patched_path=?',(str(outside),))
    with pytest.raises(MigError):
        cmd_purge(root,db,'test')
    assert outside.read_text() == 'untouched' and folder.exists()


def test_cleanup_rolls_back_files_on_move_failure(batch, monkeypatch):
    import gp_p1_mig.workflow as workflow
    root, db, original, sidecar, patched, folder = batch
    with transaction(db) as c:
        c.execute("UPDATE batches SET status='VERIFIED'")
    real_move = workflow.shutil.move
    calls = 0
    def fail_second(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('simulated')
        return real_move(src,dst)
    monkeypatch.setattr(workflow.shutil,'move',fail_second)
    with pytest.raises(OSError):
        cmd_purge(root,db,'test')
    assert folder.exists() and patched.exists()
    with transaction(db) as c:
        assert c.execute('SELECT status FROM batches').fetchone()[0] == 'VERIFIED'


def test_settings_survive_different_cwd(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    monkeypatch.setenv('GP_P1_MIG_ROOT',str(root))
    actual = tmp_path/'existing'
    actual.mkdir()
    db = actual/'custom.db'
    db.write_bytes(b'untouched database')
    settings.save_settings(actual, db)
    monkeypatch.chdir(tmp_path)
    assert settings.load_settings() == {'root':str(actual),'db':str(db)}
    assert db.read_bytes() == b'untouched database'


def test_invalid_settings_never_silently_switch_db(tmp_path,monkeypatch):
    monkeypatch.setenv('GP_P1_MIG_ROOT',str(tmp_path))
    p=settings.settings_path();p.parent.mkdir();p.write_text('{')
    with pytest.raises(ValueError):settings.load_settings()


@pytest.fixture
def web(tmp_path,monkeypatch):
    monkeypatch.setenv('GP_P1_MIG_ROOT',str(tmp_path))
    from gp_p1_mig.web import app as module
    monkeypatch.setattr(module,'STATE',{'root':str(tmp_path),'db':str(tmp_path/'data/state/state.db'),'busy':False,'current_task':None})
    monkeypatch.setattr(module,'verify_tools',lambda: {})
    return module, module.app.test_client(), {'X-App-Token':module.API_TOKEN}


def test_web_security_and_packaged_resources(web):
    module,client,headers=web
    assert client.get('/',headers={'Host':'evil.example'}).status_code == 400
    assert client.post('/api/settings',json={}).status_code == 403
    assert client.post('/api/settings',json={},headers={**headers,'Origin':'https://evil.example'}).status_code == 403
    assert client.get('/').status_code == 200
    assert client.get('/static/vendor/socket.io.min.js').status_code == 200
    assert not module.socketio.test_client(module.app).is_connected()
    socket=module.socketio.test_client(module.app,auth={'token':module.API_TOKEN})
    assert socket.is_connected()
    socket.disconnect()


def test_state_does_not_create_empty_database(web):
    module,client,_=web
    data=client.get('/api/state').get_json()
    assert data['db_error']
    assert not Path(module.STATE['db']).exists()


def test_settings_change_root_updates_db_and_rejects_busy(web,tmp_path):
    module,client,headers=web
    previous=module.STATE['db']
    result=client.post('/api/settings',json={'root':str(tmp_path/'next'),'db':previous},headers=headers)
    assert result.status_code == 200
    assert module.STATE['db'] == str(tmp_path/'next/data/state/state.db')
    module.STATE['busy']=True
    assert client.post('/api/settings',json={'root':str(tmp_path)},headers=headers).status_code == 409
    assert client.post('/api/init',json={},headers=headers).status_code == 409
