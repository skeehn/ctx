#!/usr/bin/env python3
"""
Markdown to .ctx migration tool
Converts Markdown files to ctx-vault format while preserving links and metadata.
"""

import os
import sys
import json
import hashlib
import re
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import frontmatter


def extract_frontmatter(md_content: str) -> Tuple[Dict, str]:
    """
    Extract YAML frontmatter from Markdown content.
    Returns (metadata_dict, content_without_frontmatter)
    """
    try:
        post = frontmatter.loads(md_content)
        return post.metadata, post.content
    except Exception:
        # No frontmatter found
        return {}, md_content


def compute_content_hash(content: str) -> str:
    """Compute SHA256 hash of content."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def extract_wikilinks(content: str) -> List[Dict[str, str]]:
    """
    Extract [[wikilink]] style links from content.
    Returns list of {"target": "...", "type": "see-also"} objects.
    """
    links = []
    # Pattern for [[link]] or [[link|alias]]
    pattern = r'\[\[([^|\]]+)(?:\|([^\]]+))?\]\]'
    matches = re.findall(pattern, content)
    
    for match in matches:
        target = match[0].strip()
        # Convert to .ctx filename if needed
        if not target.endswith('.ctx'):
            target = target + '.ctx'
        links.append({
            "target": target,
            "type": "see-also"
        })
    
    return links


def extract_html_links(content: str) -> List[Dict[str, str]]:
    """
    Extract standard markdown links [text](url) from content.
    Returns list of {"target": "...", "type": "see-also"} objects for .ctx files.
    """
    links = []
    # Pattern for [text](url)
    pattern = r'\[([^\]]*)\]\(([^)]+)\)'
    matches = re.findall(pattern, content)
    
    for match in matches:
        url = match[1].strip()
        # Only process links that point to .ctx files or without extension
        if url.endswith('.ctx') or '.' not in url.split('/')[-1]:
            target = url
            if not target.endswith('.ctx'):
                target = target + '.ctx'
            links.append({
                "target": target,
                "type": "see-also"
            })
    
    return links


def extract_tags_from_frontmatter(metadata: Dict) -> List[str]:
    """Extract tags from frontmatter metadata."""
    tags = []
    if 'tags' in metadata:
        tag_data = metadata['tags']
        if isinstance(tag_data, list):
            tags.extend([str(tag) for tag in tag_data])
        elif isinstance(tag_data, str):
            # Handle comma-separated tags
            tags.extend([tag.strip() for tag in tag_data.split(',')])
    return tags


def generate_ctx_header(metadata: Dict, content_hash: str, file_path: Path) -> Dict:
    """Generate the JSON header for a .ctx file."""
    header = {
        "v": 1,
        "id": f"sha256:{content_hash}",
        "updated": int(os.path.getmtime(file_path)),
        "author": metadata.get("author", "migration-tool"),
        "tags": extract_tags_from_frontmatter(metadata),
        "links": [],
        "embeddings": {}
    }
    
    # Add any custom fields from metadata that aren't standard
    standard_fields = {"author", "tags", "title"}
    for key, value in metadata.items():
        if key not in standard_fields and key not in header:
            # Store custom fields in a way that doesn't break the format
            # For now, we'll skip them to keep it simple, but could extend
            pass
    
    return header


def convert_markdown_to_ctx(md_file_path: Path, output_dir: Path, dry_run: bool = False) -> bool:
    """
    Convert a single Markdown file to .ctx format.
    Returns True if successful, False otherwise.
    """
    try:
        # Read the Markdown file
        content = md_file_path.read_text(encoding='utf-8')
        
        # Extract frontmatter and content
        metadata, body_content = extract_frontmatter(content)
        
        # Compute hash of the body content (excluding frontmatter)
        content_hash = compute_content_hash(body_content)
        
        # Extract links from content
        wikilinks = extract_wikilinks(body_content)
        htmllinks = extract_html_links(body_content)
        all_links = wikilinks + htmllinks
        
        # Generate header
        header = generate_ctx_header(metadata, content_hash, md_file_path)
        header["links"] = all_links
        
        # Build the .ctx content
        ctx_content = f"""---CTX-HEADER---
{json.dumps(header, indent=2)}
---CTX-HEADER---
{body_content}
"""
        
        # Determine output file path
        relative_path = md_file_path.relative_to(md_file_path.anchor) if md_file_path.is_absolute() else md_file_path
        # Change extension to .ctx
        ctx_file_path = output_dir / relative_path.with_suffix('.ctx')
        
        # Create parent directories if needed
        ctx_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        if dry_run:
            print(f"[DRY RUN] Would convert: {md_file_path} -> {ctx_file_path}")
            print(f"  Header: {json.dumps(header, indent=2)}")
            print(f"  Links found: {len(all_links)}")
            print(f"  Tags: {header['tags']}")
            return True
        else:
            # Write the .ctx file
            ctx_file_path.write_text(ctx_content, encoding='utf-8')
            print(f"Converted: {md_file_path} -> {ctx_file_path}")
            return True
            
    except Exception as e:
        print(f"Error converting {md_file_path}: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Convert Markdown files to ctx-vault format")
    parser.add_argument("input", help="Input file or directory")
    parser.add_argument("-o", "--output", help="Output directory (defaults to input location)")
    parser.add_argument("-r", "--recursive", action="store_true", help="Process directories recursively")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--force", action="store_true", help="Overwrite existing .ctx files")
    
    args = parser.parse_args()
    
    input_path = Path(args.input).resolve()
    
    if not input_path.exists():
        print(f"Error: Input path '{input_path}' does not exist", file=sys.stderr)
        sys.exit(1)
    
    # Determine output directory
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = input_path.parent if input_path.is_file() else input_path
    
    # Find files to process
    files_to_process = []
    if input_path.is_file():
        if input_path.suffix.lower() in ['.md', '.markdown']:
            files_to_process.append(input_path)
        else:
            print(f"Warning: Input file '{input_path}' is not a Markdown file", file=sys.stderr)
    else:
        # Directory input
        pattern = "**/*.md" if args.recursive else "*.md"
        pattern2 = "**/*.markdown" if args.recursive else "*.markdown"
        files_to_process.extend(input_path.glob(pattern))
        files_to_process.extend(input_path.glob(pattern2))
        
        # Filter to only files
        files_to_process = [f for f in files_to_process if f.is_file()]
    
    if not files_to_process:
        print("No Markdown files found to process", file=sys.stderr)
        sys.exit(1)
    
    print(f"Found {len(files_to_process)} Markdown file(s) to process")
    
    # Process each file
    success_count = 0
    for md_file in files_to_process:
        # Skip if output file exists and --force not specified
        relative_path = md_file.relative_to(md_file.anchor) if md_file.is_absolute() else md_file
        ctx_file_path = output_path / relative_path.with_suffix('.ctx')
        
        if ctx_file_path.exists() and not args.force:
            print(f"Skipping {md_file} -> {ctx_file_path} (use --force to overwrite)")
            continue
            
        if convert_markdown_to_ctx(md_file, output_path, args.dry_run):
            success_count += 1
    
    print(f"\nSuccessfully processed {success_count}/{len(files_to_process)} files")
    
    if args.dry_run:
        print("This was a dry run - no files were actually modified")


if __name__ == "__main__":
    main()