"""HTML parsing utilities for promo extraction."""
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Dict
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__)


def find_promo_pdf_links_with_images(html: str, base_url: str) -> List[Dict]:
    """
    Find all <a href="coupon.pdf"> links and extract images inside them.
    
    Returns list of dicts with:
    - pdf_url: the PDF link (for deduplication)
    - image_url: the image src/data-src/srcset
    - alt_text: image alt text
    - context: surrounding text
    """
    soup = BeautifulSoup(html, "html.parser")
    promo_links = []
    seen_pdf_urls = set()
    
    # Find all <a> tags with href ending in .pdf
    pdf_links = soup.find_all("a", href=True)
    
    for link in pdf_links:
        href = link.get("href", "")
        
        # Check if it's a PDF link (case insensitive)
        if href.lower().endswith(".pdf"):
            pdf_url = urljoin(base_url, href)
            normalized_pdf_url = pdf_url.lower().strip()
            
            # Skip duplicates
            if normalized_pdf_url in seen_pdf_urls:
                continue
            seen_pdf_urls.add(normalized_pdf_url)
            
            # Find image inside the link
            img = link.find("img")
            if not img:
                logger.debug(f"PDF link {href} has no image, skipping")
                continue
            
            # Extract image URL from src, data-src, or srcset
            image_url = None
            
            # Try src first
            if img.get("src"):
                image_url = urljoin(base_url, img.get("src"))
            # Try data-src (lazy loading)
            elif img.get("data-src"):
                image_url = urljoin(base_url, img.get("data-src"))
            # Try srcset (responsive images)
            elif img.get("srcset"):
                srcset = img.get("srcset")
                # Extract first URL from srcset (format: "url width, url2 width2")
                first_url = srcset.split(",")[0].strip().split()[0]
                image_url = urljoin(base_url, first_url)
            
            if not image_url:
                logger.debug(f"Could not extract image URL from PDF link {href}")
                continue
            
            # Get context
            alt_text = img.get("alt", "")
            context = link.get_text(strip=True)[:200]
            
            promo_links.append({
                "pdf_url": pdf_url,
                "normalized_pdf_url": normalized_pdf_url,
                "image_url": image_url,
                "alt_text": alt_text,
                "context": context,
                "link_html": str(link)[:500]
            })
    
    logger.info(f"Found {len(promo_links)} PDF links with images")
    return promo_links


def find_images_by_css_selector(html: str, base_url: str, css_selector: str) -> List[Dict]:
    """
    Find images using CSS selector (e.g., 'img.single-rebate').
    
    Returns list of dicts with:
    - image_url: the image src/data-src/data-lazy-src/data-original/data-url/srcset
    - alt_text: image alt text
    - selector: the CSS selector used
    """
    soup = BeautifulSoup(html, "html.parser")
    images = []
    seen_urls = set()
    
    # Parse CSS selector - support more complex selectors
    # Try BeautifulSoup's select() first for CSS selector support
    try:
        img_elements = soup.select(css_selector)
    except Exception:
        # Fallback to simple parsing
        parts = css_selector.split(".")
        tag = parts[0] if parts else "img"
        class_name = parts[1] if len(parts) > 1 else None
        
        # Support attribute selectors like img[class*='rebate']
        if "[" in css_selector and "]" in css_selector:
            # Parse attribute selector like img[class*='rebate']
            import re
            attr_match = re.search(r'\[([^\]]+)\]', css_selector)
            if attr_match:
                attr_str = attr_match.group(1)
                if "*=" in attr_str:  # Contains
                    attr_name, attr_value = attr_str.split("*=")
                    attr_name = attr_name.strip()
                    attr_value = attr_value.strip().strip("'\"").strip('"')
                    img_elements = soup.find_all(tag, attrs={attr_name: lambda x: x and attr_value.lower() in x.lower() if x else False})
                elif "=" in attr_str:  # Exact match
                    attr_name, attr_value = attr_str.split("=")
                    attr_name = attr_name.strip()
                    attr_value = attr_value.strip().strip("'\"").strip('"')
                    img_elements = soup.find_all(tag, attrs={attr_name: attr_value})
                else:
                    img_elements = []
            else:
                img_elements = []
        elif class_name:
            img_elements = soup.find_all(tag, class_=class_name)
        else:
            img_elements = soup.find_all(tag)
    
    for img in img_elements:
        # Try multiple attributes in order of preference
        image_url = None
        
        # Try src first
        if img.get("src"):
            image_url = urljoin(base_url, img.get("src"))
        # Try data-src (lazy loading)
        elif img.get("data-src"):
            image_url = urljoin(base_url, img.get("data-src"))
        # Try data-lazy-src
        elif img.get("data-lazy-src"):
            image_url = urljoin(base_url, img.get("data-lazy-src"))
        # Try data-original
        elif img.get("data-original"):
            image_url = urljoin(base_url, img.get("data-original"))
        # Try data-url
        elif img.get("data-url"):
            image_url = urljoin(base_url, img.get("data-url"))
        # Try srcset (responsive images)
        elif img.get("srcset"):
            srcset = img.get("srcset")
            # Extract first URL from srcset (format: "url width, url2 width2")
            first_url = srcset.split(",")[0].strip().split()[0]
            image_url = urljoin(base_url, first_url)
        
        if not image_url:
            logger.debug(f"Could not extract image URL from {css_selector} element")
            continue
        
        # Normalize URL for deduplication
        normalized_url = image_url.lower().strip()
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        
        # Get alt text
        alt_text = img.get("alt", "")
        
        images.append({
            "image_url": image_url,
            "alt_text": alt_text,
            "selector": css_selector
        })
    
    logger.info(f"Found {len(images)} images using selector '{css_selector}'")
    return images
