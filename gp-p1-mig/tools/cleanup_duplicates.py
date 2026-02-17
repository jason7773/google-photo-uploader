"""
Script to clean up duplicate files recorded in the database.
These files were skipped during ingest because they already existed,
but they still take up space in the extracted/ directory.
"""
from pathlib import Path
import os
from gp_p1_mig.db import connect

def cleanup_duplicates():
    db_path = Path('data/state/state.db')
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return

    conn = connect(db_path)
    
    # Get all duplicates
    rows = conn.execute("SELECT id, dup_path FROM duplicates").fetchall()
    print(f"Found {len(rows)} duplicate records.")
    
    deleted_count = 0
    space_freed = 0
    
    for row in rows:
        path = Path(row['dup_path'])
        if path.exists():
            try:
                size = path.stat().st_size
                path.unlink()
                deleted_count += 1
                space_freed += size
                # Try to clean up sidecar if it exists
                sidecar = path.with_name(path.name + ".json")
                if sidecar.exists():
                    sidecar.unlink()
                # Try Google's format
                sidecar2 = path.with_name(path.name + ".supplemental-metadata.json")
                if sidecar2.exists():
                    sidecar2.unlink()
                    
            except Exception as e:
                print(f"Failed to delete {path}: {e}")
        else:
            # File already gone
            pass

    # Clean up empty directories
    extracted_root = Path("data/work/extracted")
    if extracted_root.exists():
        for dirpath, dirnames, filenames in os.walk(extracted_root, topdown=False):
            if not filenames and not dirnames:
                try:
                    os.rmdir(dirpath)
                except:
                    pass

    print(f"{'='*40}")
    print(f"Cleanup Complete:")
    print(f"  Deleted: {deleted_count} files")
    print(f"  Freed: {space_freed / (1024*1024):.2f} MB")
    
    # Optional: Remove records from DB? 
    # Usually we keep them as history, but if files are gone, maybe update status?
    # For now, we just delete the files.

if __name__ == "__main__":
    cleanup_duplicates()
