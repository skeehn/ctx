"""
Backup/Restore System for ctx-vault
Provides local snapshot/backup and restore functionality for vault data.
"""
import os
import json
import shutil
import sqlite3
import tarfile
import hashlib
import time
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass, asdict
import logging

logger = logging.getLogger(__name__)


@dataclass
class BackupMetadata:
    """Metadata for a backup."""
    backup_id: str
    created_at: str
    vault_root: str
    db_path: str
    total_files: int
    total_chunks: int
    total_size_bytes: int
    schema_version: str
    notes: str = ""


class BackupManager:
    """Manages vault backups and restores."""
    
    def __init__(self, vault_root: Path, backup_dir: Optional[Path] = None):
        self.vault_root = Path(vault_root)
        self.backup_dir = backup_dir or (self.vault_root / ".backups")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Metadata file
        self.metadata_file = self.backup_dir / "backups.json"
        self._load_metadata_index()
    
    def _load_metadata_index(self):
        """Load backup metadata index."""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, "r") as f:
                    self.metadata_index = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load backup index: {e}")
                self.metadata_index = {}
        else:
            self.metadata_index = {}
    
    def _save_metadata_index(self):
        """Save backup metadata index."""
        try:
            with open(self.metadata_file, "w") as f:
                json.dump(self.metadata_index, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save backup index: {e}")
    
    def _get_db_info(self, db_path: Path) -> Dict[str, Any]:
        """Get database statistics."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            stats = {}
            stats["notes"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            stats["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            stats["links"] = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            stats["tags"] = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
            
            # Get schema version
            try:
                schema_version = conn.execute("SELECT MAX(vv) FROM files").fetchone()[0]
                stats["schema_version"] = schema_version or 1
            except:
                stats["schema_version"] = 1
            
            return stats
        finally:
            conn.close()
    
    def create_backup(self, name: str = None, notes: str = "") -> BackupMetadata:
        """
        Create a full backup of the vault.
        
        Args:
            name: Optional name for the backup
            notes: Optional notes about the backup
        
        Returns:
            BackupMetadata for the created backup
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_id = name or f"backup_{timestamp}"
        
        # Create backup directory
        backup_path = self.backup_dir / backup_id
        if backup_path.exists():
            raise ValueError(f"Backup {backup_id} already exists")
        backup_path.mkdir(parents=True)
        
        # Get DB path
        db_path_str = os.environ.get("CTX_DB_PATH", "")
        if db_path_str:
            db_path = Path(db_path_str)
        else:
            db_path = self.vault_root / "vault.db"
        
        # Copy database
        db_backup_path = backup_path / "vault.db"
        shutil.copy2(db_path, db_backup_path)
        
        # Copy all .ctx files (excluding .backups directory)
        files_dir = backup_path / "files"
        files_dir.mkdir()
        
        total_size = 0
        file_count = 0
        
        for ctx_file in self.vault_root.rglob("*.ctx"):
            # Skip files in .backups directory
            if ".backups" in ctx_file.parts:
                continue
            rel_path = ctx_file.relative_to(self.vault_root)
            dest = files_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ctx_file, dest)
            total_size += ctx_file.stat().st_size
            file_count += 1
        
        # Get DB stats
        db_stats = self._get_db_info(db_backup_path)
        
        # Create metadata
        metadata = BackupMetadata(
            backup_id=backup_id,
            created_at=datetime.now().isoformat(),
            vault_root=str(self.vault_root),
            db_path=str(db_path),
            total_files=file_count,
            total_chunks=db_stats.get("chunks", 0),
            total_size_bytes=total_size + db_backup_path.stat().st_size,
            schema_version=str(db_stats.get("schema_version", 1)),
            notes=notes,
        )
        
        # Save metadata
        meta_path = backup_path / "metadata.json"
        with open(meta_path, "w") as f:
            json.dump(asdict(metadata), f, indent=2)
        
        # Create compressed archive
        archive_path = self.backup_dir / f"{backup_id}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(backup_path, arcname=backup_id)
        
        # Remove uncompressed backup directory
        shutil.rmtree(backup_path)
        
        # Update index
        self.metadata_index[backup_id] = asdict(metadata)
        self._save_metadata_index()
        
        logger.info(f"Created backup {backup_id}: {file_count} files, {db_stats.get('chunks', 0)} chunks")
        return metadata
    
    def list_backups(self) -> List[BackupMetadata]:
        """List all available backups."""
        backups = []
        for backup_id, meta in self.metadata_index.items():
            backups.append(BackupMetadata(**meta))
        return sorted(backups, key=lambda b: b.created_at, reverse=True)
    
    def get_backup(self, backup_id: str) -> Optional[BackupMetadata]:
        """Get backup metadata by ID."""
        if backup_id in self.metadata_index:
            return BackupMetadata(**self.metadata_index[backup_id])
        return None
    
    def restore_backup(self, backup_id: str, target_dir: Optional[Path] = None, 
                       overwrite: bool = False) -> bool:
        """
        Restore a backup to the vault.
        
        Args:
            backup_id: ID of backup to restore
            target_dir: Optional target directory (default: vault_root)
            overwrite: Whether to overwrite existing files
        
        Returns:
            True if restore successful
        """
        if backup_id not in self.metadata_index:
            raise ValueError(f"Backup {backup_id} not found")
        
        metadata = self.metadata_index[backup_id]
        archive_path = self.backup_dir / f"{backup_id}.tar.gz"
        
        if not archive_path.exists():
            raise FileNotFoundError(f"Backup archive not found: {archive_path}")
        
        target = target_dir or self.vault_root
        
        if target.exists() and not overwrite:
            raise ValueError(f"Target directory {target} exists. Use overwrite=True to replace.")
        
        # Extract archive
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            
            # Extract archive
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(tmpdir)
            
            extracted_dir = tmpdir / metadata["backup_id"]
            if not extracted_dir.exists():
                raise ValueError(f"Invalid backup archive structure")
            
            # Restore database
            db_path_str = os.environ.get("CTX_DB_PATH", "")
            if db_path_str:
                db_path = Path(db_path_str)
            else:
                db_path = self.vault_root / "vault.db"
            
            # Backup current DB first
            if db_path.exists():
                backup_db = db_path.with_suffix(f".db.backup_{int(time.time())}")
                shutil.copy2(db_path, backup_db)
            
            shutil.copy2(extracted_dir / "vault.db", db_path)
            
            # Restore .ctx files
            files_src = extracted_dir / "files"
            if files_src.exists():
                # Remove existing .ctx files in vault (if overwrite)
                if overwrite:
                    for ctx_file in target.rglob("*.ctx"):
                        ctx_file.unlink()
                
                # Copy restored files
                for ctx_file in files_src.rglob("*.ctx"):
                    rel_path = ctx_file.relative_to(files_src)
                    dest = target / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ctx_file, dest)
        
        logger.info(f"Restored backup {backup_id} to {target}")
        return True
    
    def delete_backup(self, backup_id: str) -> bool:
        """Delete a backup."""
        if backup_id not in self.metadata_index:
            return False
        
        # Delete archive
        archive_path = self.backup_dir / f"{backup_id}.tar.gz"
        if archive_path.exists():
            archive_path.unlink()
        
        # Remove from index
        del self.metadata_index[backup_id]
        self._save_metadata_index()
        
        logger.info(f"Deleted backup {backup_id}")
        return True
    
    def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        """Verify backup integrity."""
        if backup_id not in self.metadata_index:
            return {"valid": False, "error": "Backup not found in index"}
        
        archive_path = self.backup_dir / f"{backup_id}.tar.gz"
        if not archive_path.exists():
            return {"valid": False, "error": "Archive file missing"}
        
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                
                with tarfile.open(archive_path, "r:gz") as tar:
                    tar.extractall(tmpdir)
                
                metadata = self.metadata_index[backup_id]
                extracted_dir = tmpdir / metadata["backup_id"]
                
                if not extracted_dir.exists():
                    return {"valid": False, "error": "Invalid archive structure"}
                
                # Verify database
                db_path = extracted_dir / "vault.db"
                if not db_path.exists():
                    return {"valid": False, "error": "Database file missing"}
                
                # Check DB integrity
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("PRAGMA integrity_check")
                finally:
                    conn.close()
                
                # Verify files directory
                files_dir = extracted_dir / "files"
                if not files_dir.exists():
                    return {"valid": False, "error": "Files directory missing"}
                
                file_count = len(list(files_dir.rglob("*.ctx")))
                
                return {
                    "valid": True,
                    "backup_id": backup_id,
                    "file_count": file_count,
                    "archive_size": archive_path.stat().st_size,
                }
        except Exception as e:
            return {"valid": False, "error": str(e)}


def create_backup_cli(vault_root: str, name: str = None, notes: str = "") -> BackupMetadata:
    """CLI helper to create a backup."""
    manager = BackupManager(Path(vault_root))
    return manager.create_backup(name, notes)


def list_backups_cli(vault_root: str) -> List[BackupMetadata]:
    """CLI helper to list backups."""
    manager = BackupManager(Path(vault_root))
    return manager.list_backups()


def restore_backup_cli(vault_root: str, backup_id: str, overwrite: bool = False) -> bool:
    """CLI helper to restore a backup."""
    manager = BackupManager(Path(vault_root))
    return manager.restore_backup(backup_id, overwrite=overwrite)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python backup.py <vault_root> <command> [args]")
        print("Commands:")
        print("  create [name] [notes]")
        print("  list")
        print("  restore <backup_id> [--overwrite]")
        print("  verify <backup_id>")
        print("  delete <backup_id>")
        sys.exit(1)
    
    vault_root = sys.argv[1]
    command = sys.argv[2]
    
    manager = BackupManager(Path(vault_root))
    
    if command == "create":
        name = sys.argv[3] if len(sys.argv) > 3 else None
        notes = sys.argv[4] if len(sys.argv) > 4 else ""
        metadata = manager.create_backup(name, notes)
        print(f"Created backup: {metadata.backup_id}")
        print(f"  Files: {metadata.total_files}")
        print(f"  Chunks: {metadata.total_chunks}")
        print(f"  Size: {metadata.total_size_bytes} bytes")
    
    elif command == "list":
        backups = manager.list_backups()
        for b in backups:
            print(f"{b.backup_id} | {b.created_at} | {b.total_files} files | {b.total_chunks} chunks | {b.total_size_bytes} bytes")
    
    elif command == "restore":
        backup_id = sys.argv[3]
        overwrite = "--overwrite" in sys.argv
        manager.restore_backup(backup_id, overwrite=overwrite)
        print(f"Restored backup {backup_id}")
    
    elif command == "verify":
        backup_id = sys.argv[3]
        result = manager.verify_backup(backup_id)
        print(json.dumps(result, indent=2))
    
    elif command == "delete":
        backup_id = sys.argv[3]
        manager.delete_backup(backup_id)
        print(f"Deleted backup {backup_id}")
    
    else:
        print(f"Unknown command: {command}")