# UNFPA Job Vacancies RSS Feed

Automatically scrapes international professional vacancies and internships from the [UNFPA careers page](https://www.unfpa.org/jobs) and publishes them as an RSS feed via GitHub Pages.

**RSS Feed URL:** [https://cinfoposte.github.io/unfpa-jobs/unfpa_jobs.xml](https://cinfoposte.github.io/unfpa-jobs/unfpa_jobs.xml)

## What it does

- Scrapes UNFPA job listings daily at 06:00 UTC using Selenium (headless Chrome)
- Filters for **international professional grades** (P-1 through P-5, D-1, D-2) and **internships/fellowships**
- Excludes consultancies, G-grade (admin/support), National Officer (NO), Service Contract (SB), and Local Service Contract (LSC) posts
- Generates a valid RSS 2.0 feed with accumulated job entries
- Commits and pushes the updated feed automatically via GitHub Actions

## Local testing

```bash
# Clone the repo
git clone https://github.com/cinfoposte/unfpa-jobs.git
cd unfpa-jobs

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the scraper (requires Chrome/Chromium installed locally)
python scraper.py
```

The output will be written to `unfpa_jobs.xml` in the repo root.

## GitHub Pages activation

1. Go to **Settings** → **Pages**
2. Under **Source**, select **Deploy from a branch**
3. Set the branch to **main** and folder to **/ (root)**
4. Click **Save**

The feed will be available at: `https://cinfoposte.github.io/unfpa-jobs/unfpa_jobs.xml`

## cinfoPoste import mapping

| Portal-Feld | Dropdown-Auswahl |
|-------------|-----------------|
| TITLE       | → Title         |
| LINK        | → Link          |
| DESCRIPTION | → Description   |
| PUBDATE     | → Date          |
| ITEM        | → Start item    |
| GUID        | → Unique ID     |
