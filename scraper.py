#!/usr/bin/env python3
"""
UNFPA Job Vacancies Scraper
Scrapes https://www.unfpa.org/jobs using Selenium, filters by grade/type,
and generates/updates an RSS feed in unfpa_jobs.xml.
"""

import hashlib
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.dom import minidom

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://www.unfpa.org/jobs"
RSS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unfpa_jobs.xml")
MAX_INCLUDED = 50
WAIT_SECONDS = 20

INCLUDED_GRADES = {"P-1", "P-2", "P-3", "P-4", "P-5", "D-1", "D-2"}
EXCLUDED_GRADE_PREFIXES = ("G-", "SB-", "LSC-")
EXCLUDED_NO_GRADES = {"NOA", "NOB", "NOC", "NOD",
                       "NO-A", "NO-B", "NO-C", "NO-D"}
CONSULTANT_KEYWORDS = ["CONSULTANT", "CONSULTANCY"]
INTERN_KEYWORDS = ["INTERN", "FELLOWSHIP", "FELLOW"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def generate_numeric_id(url: str) -> str:
    """Generate a 16-digit numeric GUID from a URL via MD5."""
    hex_dig = hashlib.md5(url.encode()).hexdigest()
    return str(int(hex_dig[:16], 16) % 10000000000000000).zfill(16)


def normalize(text: str) -> str:
    """Normalize a string for grade/keyword comparison."""
    if not text:
        return ""
    # Replace unicode dashes with ASCII hyphen
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]", "-", text)
    text = text.upper().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def expand_compact_grade(text: str) -> str:
    """Expand compact grade forms like P4 -> P-4, D1 -> D-1, G6 -> G-6, etc."""
    # Match letter(s) followed directly by digit(s), insert hyphen
    # Covers P4, D1, G6, SB1, LSC10, NOA etc.
    return re.sub(r"\b(P|D|G|SB|LSC|NO)(\d+)\b", r"\1-\2", text)


def expand_spaced_grade(text: str) -> str:
    """Expand spaced grade forms like 'P 4' -> 'P-4', 'G 6' -> 'G-6'."""
    return re.sub(r"\b(P|D|G|SB|LSC|NO)\s+(\d+)\b", r"\1-\2", text)


def normalize_grade(raw: str) -> str:
    """Full normalization pipeline for a grade string."""
    n = normalize(raw)
    n = expand_compact_grade(n)
    n = expand_spaced_grade(n)
    return n


def contains_consultant(text: str) -> bool:
    n = normalize(text)
    return any(kw in n for kw in CONSULTANT_KEYWORDS)


def contains_intern(text: str) -> bool:
    n = normalize(text)
    return any(kw in n for kw in INTERN_KEYWORDS)


def is_excluded_grade(grade_norm: str) -> bool:
    """Check if grade matches any excluded group (G-*, SB-*, LSC-*, NO*)."""
    for prefix in EXCLUDED_GRADE_PREFIXES:
        if prefix in grade_norm:
            # Check it is actually a grade token
            pattern = re.compile(r"\b" + re.escape(prefix) + r"\d+\b")
            if pattern.search(grade_norm):
                return True
    for no in EXCLUDED_NO_GRADES:
        if no in grade_norm:
            return True
    return False


def is_included_grade(grade_norm: str) -> bool:
    for g in INCLUDED_GRADES:
        if g in grade_norm:
            return True
    return False


def should_include(title: str, grade: str, contract_type: str, category: str) -> bool:
    """
    Decision logic (priority order):
    1) Consultant detected anywhere -> EXCLUDE
    2) Grade in excluded group -> EXCLUDE
    3) Grade in included set -> INCLUDE
    4) Internship/fellowship in grade/contract/category -> INCLUDE
    5) Title contains intern/fellow -> INCLUDE
    6) Else -> EXCLUDE
    """
    all_fields = f"{title} {grade} {contract_type} {category}"

    # 1) Consultant check across ALL fields
    if contains_consultant(all_fields):
        return False

    grade_norm = normalize_grade(grade)
    contract_norm = normalize_grade(contract_type)
    category_norm = normalize(category)

    # 2) Excluded grades
    if is_excluded_grade(grade_norm):
        return False

    # 3) Included grades
    if is_included_grade(grade_norm):
        return True

    # 4) Internship/fellowship in grade/contract/category
    if contains_intern(grade) or contains_intern(contract_type) or contains_intern(category):
        return True

    # 5) Fallback: title contains intern/fellow
    if contains_intern(title):
        return True

    # 6) Default exclude
    return False


# ---------------------------------------------------------------------------
# Selenium setup
# ---------------------------------------------------------------------------
def create_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    return driver


# ---------------------------------------------------------------------------
# Scraping strategies
# ---------------------------------------------------------------------------
def extract_text(el, selector):
    """Safely extract text from a child element."""
    try:
        child = el.find_element(By.CSS_SELECTOR, selector)
        return child.text.strip()
    except Exception:
        return ""


def extract_field_by_label(el, label):
    """Extract a field value by looking for a label element."""
    try:
        labels = el.find_elements(By.XPATH, f".//*[contains(text(),'{label}')]")
        for lbl in labels:
            parent = lbl.find_element(By.XPATH, "..")
            full = parent.text.strip()
            # Remove the label text to get the value
            val = full.replace(label, "").strip().lstrip(":").strip()
            if val:
                return val
        return ""
    except Exception:
        return ""


def strategy_a(driver):
    """Find job cards by 'View' links that contain /jobs/ hrefs."""
    jobs = []
    try:
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']")
        seen = set()
        for link in links:
            href = link.get_attribute("href") or ""
            if "/jobs/" not in href or href in seen:
                continue
            # Skip pagination / filter links
            if href.rstrip("/") == BASE_URL.rstrip("/"):
                continue
            if "?" in href and "/jobs/" not in href.split("?")[0]:
                continue
            seen.add(href)

            # Try to get the parent card/container
            title_text = ""
            location = ""
            grade = ""
            contract_type = ""
            closing_date = ""
            category = ""

            # The link text itself might be the title or "View"
            link_text = link.text.strip()

            # Try walking up to find a container
            container = None
            try:
                container = link.find_element(By.XPATH, "./ancestor::div[contains(@class,'views-row') or contains(@class,'job') or contains(@class,'card') or contains(@class,'item') or contains(@class,'row')]")
            except Exception:
                try:
                    container = link.find_element(By.XPATH, "./ancestor::tr")
                except Exception:
                    try:
                        container = link.find_element(By.XPATH, "./ancestor::li")
                    except Exception:
                        pass

            if container:
                full_text = container.text
                # Try to extract title - first heading or strong text
                for sel in ["h2", "h3", "h4", ".title", "strong", "a[href*='/jobs/']"]:
                    t = extract_text(container, sel)
                    if t and len(t) > 5 and t.lower() != "view":
                        title_text = t
                        break

                # Extract metadata fields
                location = extract_field_by_label(container, "Location")
                if not location:
                    location = extract_field_by_label(container, "Duty Station")
                grade = extract_field_by_label(container, "Grade")
                if not grade:
                    grade = extract_field_by_label(container, "Staff grade")
                if not grade:
                    grade = extract_field_by_label(container, "Level")
                contract_type = extract_field_by_label(container, "Contract")
                if not contract_type:
                    contract_type = extract_field_by_label(container, "Type")
                closing_date = extract_field_by_label(container, "Closing")
                if not closing_date:
                    closing_date = extract_field_by_label(container, "Deadline")
                category = extract_field_by_label(container, "Category")
                if not category:
                    category = extract_field_by_label(container, "Job category")
            else:
                if link_text and len(link_text) > 5 and link_text.lower() != "view":
                    title_text = link_text

            if not title_text:
                title_text = link_text if link_text and link_text.lower() != "view" else ""

            if not title_text or len(title_text) < 5:
                # Try to extract from URL
                slug = href.rstrip("/").split("/")[-1]
                title_text = slug.replace("-", " ").title()

            if title_text and len(title_text) >= 5:
                jobs.append({
                    "title": title_text,
                    "link": href,
                    "location": location,
                    "grade": grade,
                    "contract_type": contract_type,
                    "closing_date": closing_date,
                    "category": category,
                })
    except Exception as e:
        print(f"[Strategy A] Error: {e}")
    return jobs


def strategy_b(driver):
    """Scan all anchors for /jobs/ hrefs and extract title from link text or surrounding."""
    jobs = []
    try:
        anchors = driver.find_elements(By.TAG_NAME, "a")
        seen = set()
        for a in anchors:
            href = a.get_attribute("href") or ""
            if "/jobs/" not in href or href in seen:
                continue
            if href.rstrip("/") == BASE_URL.rstrip("/"):
                continue
            seen.add(href)

            text = a.text.strip()
            if not text or len(text) < 5 or text.lower() == "view":
                slug = href.rstrip("/").split("/")[-1]
                text = slug.replace("-", " ").title()

            if text and len(text) >= 5:
                jobs.append({
                    "title": text,
                    "link": href,
                    "location": "",
                    "grade": "",
                    "contract_type": "",
                    "closing_date": "",
                    "category": "",
                })
    except Exception as e:
        print(f"[Strategy B] Error: {e}")
    return jobs


def strategy_c(driver):
    """Locate blocks with Location/Grade/Contract labels."""
    jobs = []
    try:
        # Find elements that contain typical job metadata labels
        containers = driver.find_elements(
            By.XPATH,
            "//*[contains(text(),'Location') or contains(text(),'Staff grade')]"
            "/ancestor::div[.//a[contains(@href,'/jobs/')]]"
        )
        seen = set()
        for container in containers:
            try:
                link_el = container.find_element(By.CSS_SELECTOR, "a[href*='/jobs/']")
                href = link_el.get_attribute("href") or ""
                if href in seen or href.rstrip("/") == BASE_URL.rstrip("/"):
                    continue
                seen.add(href)

                title_text = ""
                for sel in ["h2", "h3", "h4", ".title", "strong"]:
                    t = extract_text(container, sel)
                    if t and len(t) > 5:
                        title_text = t
                        break
                if not title_text:
                    title_text = link_el.text.strip()
                if not title_text or len(title_text) < 5:
                    slug = href.rstrip("/").split("/")[-1]
                    title_text = slug.replace("-", " ").title()

                location = extract_field_by_label(container, "Location")
                grade = extract_field_by_label(container, "Grade")
                if not grade:
                    grade = extract_field_by_label(container, "Staff grade")
                contract_type = extract_field_by_label(container, "Contract")
                closing_date = extract_field_by_label(container, "Closing")
                category = extract_field_by_label(container, "Category")

                jobs.append({
                    "title": title_text,
                    "link": href,
                    "location": location,
                    "grade": grade,
                    "contract_type": contract_type,
                    "closing_date": closing_date,
                    "category": category,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"[Strategy C] Error: {e}")
    return jobs


def merge_jobs(lists):
    """Merge multiple job lists, preferring entries with more metadata."""
    by_link = {}
    for jobs in lists:
        for job in jobs:
            link = job["link"]
            if link not in by_link:
                by_link[link] = job
            else:
                existing = by_link[link]
                # Prefer the entry with more filled fields
                existing_filled = sum(1 for v in existing.values() if v)
                new_filled = sum(1 for v in job.values() if v)
                if new_filled > existing_filled:
                    by_link[link] = job
    return list(by_link.values())


def find_next_page(driver):
    """Try to find and click a 'next' pagination link. Returns True if navigated."""
    try:
        # Common pagination patterns
        selectors = [
            "li.pager-next a",
            "a.pager-next",
            ".pagination .next a",
            "a[rel='next']",
            ".pager__item--next a",
            "li.next a",
            "a[title='Go to next page']",
        ]
        for sel in selectors:
            try:
                next_btn = driver.find_element(By.CSS_SELECTOR, sel)
                if next_btn and next_btn.is_displayed():
                    next_btn.click()
                    time.sleep(5)
                    return True
            except Exception:
                continue

        # Fallback: look for "Next" / "›" / ">>" text in links
        links = driver.find_elements(By.TAG_NAME, "a")
        for link in links:
            text = link.text.strip().lower()
            if text in ("next", "next ›", "next »", "›", "»", ">>"):
                try:
                    link.click()
                    time.sleep(5)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def scrape_jobs():
    """Main scraping function. Returns a list of included job dicts."""
    driver = create_driver()
    included = []
    page = 0

    try:
        print(f"[Scraper] Loading {BASE_URL}")
        driver.get(BASE_URL)

        # Wait for JS rendering
        print(f"[Scraper] Waiting {WAIT_SECONDS}s for JS rendering...")
        time.sleep(WAIT_SECONDS)

        while len(included) < MAX_INCLUDED:
            page += 1
            print(f"\n[Scraper] Processing page {page}")

            # Try all strategies
            jobs_a = strategy_a(driver)
            jobs_b = strategy_b(driver)
            jobs_c = strategy_c(driver)

            print(f"  Strategy A found {len(jobs_a)} jobs")
            print(f"  Strategy B found {len(jobs_b)} jobs")
            print(f"  Strategy C found {len(jobs_c)} jobs")

            all_jobs = merge_jobs([jobs_a, jobs_b, jobs_c])
            print(f"  Merged: {len(all_jobs)} unique jobs")

            if not all_jobs:
                print("  No jobs found on this page, stopping.")
                break

            new_on_page = 0
            for job in all_jobs:
                if len(included) >= MAX_INCLUDED:
                    break

                title = job["title"]
                grade = job.get("grade", "")
                contract_type = job.get("contract_type", "")
                category = job.get("category", "")

                if should_include(title, grade, contract_type, category):
                    included.append(job)
                    new_on_page += 1
                    print(f"  + INCLUDED: {title} (grade={grade}, contract={contract_type})")
                else:
                    print(f"  - excluded: {title} (grade={grade}, contract={contract_type})")

            print(f"  Included {new_on_page} from page {page} (total: {len(included)})")

            if len(included) >= MAX_INCLUDED:
                break

            # Try next page
            if not find_next_page(driver):
                print("  No more pages.")
                break

            print(f"  Navigated to next page, waiting...")
            time.sleep(WAIT_SECONDS)

    except Exception as e:
        print(f"[Scraper] Fatal error: {e}")
    finally:
        driver.quit()

    print(f"\n[Scraper] Total included jobs: {len(included)}")
    return included


# ---------------------------------------------------------------------------
# RSS generation
# ---------------------------------------------------------------------------
def load_existing_feed():
    """Load existing RSS feed and return dict of link->item_dict."""
    existing = {}
    if not os.path.exists(RSS_FILE):
        return existing
    try:
        tree = ET.parse(RSS_FILE)
        root = tree.getroot()
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        channel = root.find("channel")
        if channel is None:
            return existing
        for item in channel.findall("item"):
            link_el = item.find("link")
            if link_el is not None and link_el.text:
                link = link_el.text.strip()
                existing[link] = {
                    "title": (item.find("title").text or "").strip() if item.find("title") is not None else "",
                    "link": link,
                    "description": (item.find("description").text or "").strip() if item.find("description") is not None else "",
                    "guid": (item.find("guid").text or "").strip() if item.find("guid") is not None else "",
                    "pubDate": (item.find("pubDate").text or "").strip() if item.find("pubDate") is not None else "",
                }
    except Exception as e:
        print(f"[RSS] Error loading existing feed: {e}")
    return existing


def build_description(job):
    """Build CDATA description text for a job."""
    title = job.get("title", "")
    location = job.get("location", "Unknown")
    grade = job.get("grade", "")
    contract_type = job.get("contract_type", "")
    closing_date = job.get("closing_date", "")

    desc = f"UNFPA has a vacancy for the position of {title}. Location: {location}."
    if grade:
        desc += f" Grade: {grade}."
    if contract_type:
        desc += f" Contract type: {contract_type}."
    if closing_date:
        desc += f" Closing date: {closing_date}."
    return desc


def generate_rss(new_jobs):
    """Generate/update the RSS feed XML file."""
    now_rfc2822 = format_datetime(datetime.now(timezone.utc))

    # Load existing items
    existing = load_existing_feed()
    existing_links = set(existing.keys())
    print(f"[RSS] Existing items in feed: {len(existing)}")

    # Count truly new
    truly_new = [j for j in new_jobs if j["link"] not in existing_links]
    print(f"[RSS] New items to add: {len(truly_new)}")

    # Build XML
    NSMAP_DC = "http://purl.org/dc/elements/1.1/"
    NSMAP_ATOM = "http://www.w3.org/2005/Atom"

    ET.register_namespace("dc", NSMAP_DC)
    ET.register_namespace("atom", NSMAP_ATOM)

    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:dc", NSMAP_DC)
    rss.set("xmlns:atom", NSMAP_ATOM)

    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "UNFPA Job Vacancies"
    ET.SubElement(channel, "link").text = "https://www.unfpa.org/jobs"
    ET.SubElement(channel, "description").text = "List of vacancies at UNFPA"
    ET.SubElement(channel, "language").text = "en"

    atom_link = ET.SubElement(channel, "atom:link")
    atom_link.set("href", "https://cinfoposte.github.io/unfpa-jobs/unfpa_jobs.xml")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    ET.SubElement(channel, "pubDate").text = now_rfc2822

    # Add existing items
    for link, data in existing.items():
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = data.get("title", "")
        ET.SubElement(item, "link").text = data["link"]
        ET.SubElement(item, "description").text = data.get("description", "")
        guid = ET.SubElement(item, "guid")
        guid.set("isPermaLink", "false")
        guid.text = data.get("guid", generate_numeric_id(link))
        ET.SubElement(item, "pubDate").text = data.get("pubDate", now_rfc2822)
        source = ET.SubElement(item, "source")
        source.set("url", "https://www.unfpa.org/jobs")
        source.text = "UNFPA Job Vacancies"

    # Add new items
    for job in truly_new:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = job["title"]
        ET.SubElement(item, "link").text = job["link"]
        ET.SubElement(item, "description").text = build_description(job)
        guid = ET.SubElement(item, "guid")
        guid.set("isPermaLink", "false")
        guid.text = generate_numeric_id(job["link"])
        ET.SubElement(item, "pubDate").text = now_rfc2822
        source = ET.SubElement(item, "source")
        source.set("url", "https://www.unfpa.org/jobs")
        source.text = "UNFPA Job Vacancies"

    # Convert to string with CDATA for descriptions
    rough_xml = ET.tostring(rss, encoding="unicode", xml_declaration=False)

    # Use minidom for pretty printing and CDATA handling
    dom = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{rough_xml}')

    # Replace description text nodes with CDATA sections
    for desc_node in dom.getElementsByTagName("description"):
        if desc_node.firstChild and desc_node.firstChild.nodeType == desc_node.TEXT_NODE:
            text = desc_node.firstChild.nodeValue
            desc_node.removeChild(desc_node.firstChild)
            cdata = dom.createCDATASection(text)
            desc_node.appendChild(cdata)

    xml_str = dom.toprettyxml(indent="  ", encoding=None)
    # Remove extra blank lines from minidom output
    lines = [line for line in xml_str.split("\n") if line.strip()]
    xml_str = "\n".join(lines) + "\n"

    with open(RSS_FILE, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"[RSS] Feed written to {RSS_FILE}")
    total = len(existing) + len(truly_new)
    print(f"[RSS] Total items in feed: {total}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("UNFPA Job Vacancies Scraper")
    print("=" * 60)
    jobs = scrape_jobs()
    generate_rss(jobs)
    print("\nDone.")
