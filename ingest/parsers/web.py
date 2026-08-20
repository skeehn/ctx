"""
Web parser: extracts article content and metadata from HTML using readability-lxml.
Enhanced with table extraction, code block detection, and structure preservation.
"""
import re
from typing import Dict, Any, List, Optional
from datetime import datetime
from urllib.parse import urljoin, urlparse
import hashlib
from pathlib import Path

from readability import Document
from bs4 import BeautifulSoup
import trafilatura  # alternative, but we'll use readability as primary


def parse_web_html(html: str, base_url: str = "") -> Dict[str, Any]:
    """
    Parse HTML and return structured data for .ctx conversion.
    Returns dict with keys: title, authors, date, source_url, tags, chunks (list of dicts).
    Chunks will include: text, table, code, figure, heading, list types.
    """
    if not html:
        return _empty_result()
    
    # Extract tables from ORIGINAL HTML before readability (readability often strips tables)
    tables_from_original = []
    try:
        soup_orig = BeautifulSoup(html, "html.parser")
        for table in soup_orig.find_all("table"):
            table_data = _parse_table(table)
            if table_data:
                tables_from_original.append({
                    "type": "table",
                    "content": table_data["markdown"],
                    "headers": table_data.get("headers", []),
                    "rows": table_data.get("rows", []),
                })
    except Exception:
        pass
    
    # Use readability to get article summary
    try:
        doc = Document(html)
        article_html = doc.summary()  # returns HTML string of the article
        title = doc.short_title() or doc.title() or ""
    except Exception:
        # Fallback: use BeautifulSoup to get title and remove obvious boilerplate
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title else ""
        # Remove script, style, nav, header, footer, aside
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        article_html = str(soup.body) if soup.body else str(soup)
    
    # Parse article with enhanced structure extraction
    soup_article = BeautifulSoup(article_html, "html.parser")
    
    # Extract structured chunks
    chunks = _extract_structured_chunks(soup_article)
    
    # If no structured chunks found, fall back to full text
    if not chunks:
        text = soup_article.get_text(separator="\n", strip=True)
        chunks = [{"type": "text", "content": text}]
    
    # Add tables extracted from original HTML (if any)
    if tables_from_original:
        # Add ordinals to tables
        max_ordinal = max((c.get("ordinal", 0) for c in chunks), default=-1)
        for i, table in enumerate(tables_from_original):
            table["ordinal"] = max_ordinal + 1 + i
        chunks.extend(tables_from_original)
    
    # Try to extract metadata: author, date
    authors = []
    publish_date = None
    # Try with trafilatura for metadata (if installed)
    try:
        import trafilatura
        metadata = trafilatura.extract_metadata(html, default_url=base_url or None)
        if metadata:
            if metadata.author:
                authors = [metadata.author] if isinstance(metadata.author, str) else list(metadata.author)
            if metadata.date:
                publish_date = metadata.date
    except Exception:
        pass
    
    # If still no authors, try to find common patterns
    if not authors:
        # Look for meta tags
        soup_meta = BeautifulSoup(html, "html.parser")
        # author meta
        author_meta = soup_meta.find("meta", {"name": "author"}) or soup_meta.find("meta", {"property": "article:author"})
        if author_meta and author_meta.get("content"):
            authors = [author_meta["content"].strip()]
        # Open Graph article:author
        if not authors:
            og_author = soup_meta.find("meta", {"property": "og:article:author"})
            if og_author and og_author.get("content"):
                authors = [og_author["content"].strip()]
    
    # Date extraction
    if not publish_date:
        soup_meta = BeautifulSoup(html, "html.parser")
        # common meta tags
        for prop in ["article:published_time", "og:published_time", "date", "DC.date.issued"]:
            tag = soup_meta.find("meta", {"property": prop}) or soup_meta.find("meta", {"name": prop})
            if tag and tag.get("content"):
                publish_date = tag["content"]
                break
        # Also try <time> tag
        if not publish_date:
            time_tag = soup_meta.find("time")
            if time_tag and time_tag.has_attr("datetime"):
                publish_date = time_tag["datetime"]
    
    # Parse date string to ISO format if possible
    date_str = None
    if publish_date:
        try:
            # If it's already a datetime object (from trafilatura)
            if isinstance(publish_date, datetime):
                date_str = publish_date.isoformat()
            else:
                # Try parsing common formats
                from dateutil.parser import parse as date_parse
                dt = date_parse(publish_date)
                date_str = dt.isoformat()
        except Exception:
            date_str = str(publish_date)  # keep as is
    
    # Tags: extract from meta keywords or og:tag
    tags = []
    try:
        soup_meta = BeautifulSoup(html, "html.parser")
        # meta keywords
        kw = soup_meta.find("meta", {"name": "keywords"})
        if kw and kw.get("content"):
            tags = [t.strip() for t in kw["content"].split(",") if t.strip()]
        # og:tag (can be multiple)
        og_tags = soup_meta.find_all("meta", {"property": "og:tag"})
        for tag in og_tags:
            if tag.get("content"):
                tags.append(tag["content"].strip())
    except Exception:
        pass
    
    # Source URL
    source_url = base_url or ""
    
    return {
        "title": title.strip(),
        "authors": authors,
        "date": date_str,
        "source_url": source_url,
        "tags": list(set(tags)),  # deduplicate
        "chunks": chunks,
    }


def _extract_structured_chunks(soup: BeautifulSoup) -> List[Dict]:
    """Extract structured chunks from article soup: text, tables, code, figures, headings, lists."""
    chunks = []
    ordinal = 0
    
    # Find all content elements in order
    for element in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "table", "pre", "ul", "ol", "figure", "img", "blockquote", "hr"]):
        # Skip empty elements
        if not element.get_text(strip=True) and element.name not in ["img", "hr"]:
            continue
        
        if element.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            chunks.append({
                "type": "heading",
                "level": int(element.name[1]),
                "ordinal": ordinal,
                "content": element.get_text(strip=True),
            })
            ordinal += 1
            
        elif element.name == "p":
            text = element.get_text(strip=True)
            if text:
                chunks.append({
                    "type": "text",
                    "ordinal": ordinal,
                    "content": text,
                })
                ordinal += 1
                
        elif element.name == "table":
            table_data = _parse_table(element)
            if table_data:
                chunks.append({
                    "type": "table",
                    "ordinal": ordinal,
                    "content": table_data,  # Markdown format
                    "headers": table_data.get("headers", []),
                    "rows": table_data.get("rows", []),
                })
                ordinal += 1
                
        elif element.name == "pre":
            code_content, language = _extract_code_block(element)
            if code_content:
                chunks.append({
                    "type": "code",
                    "ordinal": ordinal,
                    "content": code_content,
                    "language": language,
                })
                ordinal += 1
                
        elif element.name in ["ul", "ol"]:
            list_items = [li.get_text(strip=True) for li in element.find_all("li", recursive=False)]
            if list_items:
                chunks.append({
                    "type": "list",
                    "ordinal": ordinal,
                    "content": "\n".join(f"- {item}" for item in list_items) if element.name == "ul" else "\n".join(f"{i+1}. {item}" for i, item in enumerate(list_items)),
                    "ordered": element.name == "ol",
                })
                ordinal += 1
                
        elif element.name == "figure":
            fig_data = _parse_figure(element)
            if fig_data:
                chunks.append({
                    "type": "figure",
                    "ordinal": ordinal,
                    **fig_data,
                })
                ordinal += 1
                
        elif element.name == "img":
            img_data = _parse_image(element)
            if img_data:
                chunks.append({
                    "type": "figure",
                    "ordinal": ordinal,
                    **img_data,
                })
                ordinal += 1
                
        elif element.name == "blockquote":
            text = element.get_text(strip=True)
            if text:
                chunks.append({
                    "type": "quote",
                    "ordinal": ordinal,
                    "content": text,
                })
                ordinal += 1
                
        elif element.name == "hr":
            chunks.append({
                "type": "divider",
                "ordinal": ordinal,
                "content": "---",
            })
            ordinal += 1
    
    return chunks


def _parse_table(table: BeautifulSoup) -> Optional[Dict]:
    """Parse HTML table to Markdown format with headers and rows."""
    try:
        # Extract headers
        headers = []
        thead = table.find("thead")
        if thead:
            for th in thead.find_all("th"):
                headers.append(th.get_text(strip=True))
        else:
            # First row might be headers
            first_row = table.find("tr")
            if first_row:
                for th in first_row.find_all(["th", "td"]):
                    headers.append(th.get_text(strip=True))
        
        # Extract rows
        rows = []
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        
        # Skip header row if it's duplicated in rows
        if headers and rows and rows[0] == headers:
            rows = rows[1:]
        
        # Build Markdown table
        if headers:
            md = "| " + " | ".join(headers) + " |\n"
            md += "| " + " | ".join("---" for _ in headers) + " |\n"
            for row in rows:
                # Pad row to match header count
                padded = row + [""] * (len(headers) - len(row))
                md += "| " + " | ".join(padded[:len(headers)]) + " |\n"
        else:
            md = "\n".join("| " + " | ".join(row) + " |" for row in rows)
        
        return {"markdown": md, "headers": headers, "rows": rows}
    except Exception:
        return None


def _extract_code_block(pre: BeautifulSoup) -> tuple[str, str]:
    """Extract code content and detect language from <pre><code> block."""
    code_tag = pre.find("code")
    if code_tag:
        # Try to detect language from class
        language = ""
        for cls in code_tag.get("class", []):
            if cls.startswith("language-") or cls.startswith("lang-"):
                language = cls.split("-")[-1]
                break
        content = code_tag.get_text()
        return content, language
    return pre.get_text(), ""


def _parse_figure(figure: BeautifulSoup) -> Optional[Dict]:
    """Parse <figure> element with optional <figcaption> and <img>."""
    img = figure.find("img")
    figcaption = figure.find("figcaption")
    
    caption = figcaption.get_text(strip=True) if figcaption else ""
    src = img.get("src") if img else ""
    alt = img.get("alt", "") if img else ""
    
    if src:
        return {
            "caption": caption or alt,
            "image_url": src,
            "content": f"![{caption or alt}]({src})",
        }
    return None


def _parse_image(img: BeautifulSoup) -> Optional[Dict]:
    """Parse standalone <img> element."""
    src = img.get("src")
    alt = img.get("alt", "")
    if src:
        return {
            "caption": alt,
            "image_url": src,
            "content": f"![{alt}]({src})",
        }
    return None


def _empty_result() -> Dict[str, Any]:
    return {
        "title": "",
        "authors": [],
        "date": None,
        "source_url": "",
        "tags": [],
        "chunks": [],
    }


# For testing
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python web.py <html-file>")
        sys.exit(1)
    html = Path(sys.argv[1]).read_text(encoding="utf-8")
    result = parse_web_html(html)
    import json
    print(json.dumps(result, indent=2))