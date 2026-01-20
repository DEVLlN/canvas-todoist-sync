# Canvas to Todoist Sync

[![Sync Status](https://github.com/DEVLlN/canvas-todoist-sync/actions/workflows/sync.yml/badge.svg)](https://github.com/DEVLlN/canvas-todoist-sync/actions/workflows/sync.yml)

Automatically sync your Canvas LMS assignments to Todoist tasks. This tool:

- Fetches assignments from your Canvas calendar feed (ICS format)
- Creates Todoist tasks with proper due dates
- Organizes assignments with labels by course name
- Sets priority based on how soon assignments are due
- Runs automatically every hour via GitHub Actions
- Tracks synced assignments to prevent duplicates

## Quick Start (GitHub Actions)

### 1. Fork or Clone This Repository

```bash
git clone https://github.com/YOUR_USERNAME/canvas-todoist-sync.git
cd canvas-todoist-sync
```

Or click "Use this template" / "Fork" on GitHub.

### 2. Get Your Canvas Calendar URL

1. Log into Canvas
2. Go to **Calendar** (left sidebar)
3. Click **Calendar Feed** (right side of the page)
4. Copy the URL - it looks like:
   ```
   https://your-school.instructure.com/feeds/calendars/user_XXXXXX.ics
   ```

### 3. Get Your Todoist API Token

1. Go to [Todoist Integrations Settings](https://todoist.com/prefs/integrations)
2. Scroll down to **API token**
3. Copy the token

### 4. Add Secrets to GitHub

1. Go to your repository on GitHub
2. Click **Settings** -> **Secrets and variables** -> **Actions**
3. Click **New repository secret** and add:

   | Name | Value |
   |------|-------|
   | `TODOIST_API_TOKEN` | Your Todoist API token |
   | `CANVAS_ICS_URL` | Your Canvas calendar feed URL |

### 5. Enable GitHub Actions

1. Go to the **Actions** tab in your repository
2. Click **I understand my workflows, go ahead and enable them**
3. The sync will now run automatically every hour

### 6. Test It

1. Go to **Actions** tab
2. Select **Canvas to Todoist Sync**
3. Click **Run workflow** -> **Run workflow**
4. Check your Todoist for new tasks!

## How It Works

### Task Organization

- All assignments go into a **"Canvas Assignments"** project in Todoist
- Each course gets its own **label** (e.g., `CHEM_350`, `MATH_241`)
- You can filter by label to see assignments for specific courses

### Priority System

Tasks are automatically prioritized based on due date:

| Due Date | Todoist Priority |
|----------|------------------|
| Within 1 day | P1 (Urgent/Red) |
| Within 3 days | P2 (High/Orange) |
| Within 7 days | P3 (Medium/Yellow) |
| Later | P4 (Normal) |

### Deduplication

The sync tracks assignments by their unique Canvas ID to prevent duplicates:
- New assignments are created as tasks
- Changed assignments (title, due date, description) are updated
- Already-synced, unchanged assignments are skipped
- Past assignments are ignored

## Local Development

### Setup

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/canvas-todoist-sync.git
cd canvas-todoist-sync

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment file
cp .env.example .env
# Edit .env with your actual values
```

### Run Manually

```bash
# With .env file
source venv/bin/activate
export $(cat .env | xargs)
python sync.py

# Or set environment variables directly
TODOIST_API_TOKEN="your_token" CANVAS_ICS_URL="your_url" python sync.py
```

## Configuration Options

Set these as environment variables or GitHub secrets:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TODOIST_API_TOKEN` | Yes | - | Your Todoist API token |
| `CANVAS_ICS_URL` | Yes | - | Your Canvas calendar feed URL |
| `TODOIST_PROJECT_NAME` | No | `Canvas Assignments` | Name of Todoist project |
| `STATE_FILE` | No | `sync_state.json` | Path to state tracking file |

## Customization

### Change Sync Frequency

Edit `.github/workflows/sync.yml` and modify the cron schedule:

```yaml
schedule:
  # Every 30 minutes
  - cron: '*/30 * * * *'

  # Every 2 hours
  - cron: '0 */2 * * *'

  # Every 6 hours
  - cron: '0 */6 * * *'

  # Once daily at 8 AM UTC
  - cron: '0 8 * * *'
```

### Change Priority Thresholds

Edit `sync.py` and modify the `PRIORITY_THRESHOLDS` dictionary:

```python
PRIORITY_THRESHOLDS = {
    1: 4,   # Due within 1 day -> urgent
    3: 3,   # Due within 3 days -> high
    7: 2,   # Due within 7 days -> medium
}
```

## Troubleshooting

### Tasks Not Appearing

1. Check that your secrets are correctly set in GitHub
2. Look at the Actions tab for error logs
3. Verify your Canvas calendar URL works by opening it in a browser
4. Make sure there are future assignments in Canvas

### Duplicate Tasks

The sync state is cached between runs. If you see duplicates:
1. Go to Actions -> Clear all caches
2. Manually delete duplicate tasks in Todoist
3. Run the workflow again

### Wrong Course Labels

Course names are extracted from Canvas event titles. If they look wrong:
1. Check how assignments appear in your Canvas calendar
2. You may need to adjust the `parse_course_name()` function in `sync.py`

## Security Notes

- **Never commit your API tokens** - always use GitHub Secrets or environment variables
- Your Canvas ICS URL contains a private token - keep it confidential
- The `.gitignore` file prevents accidental commits of `.env` files

## License

MIT License - feel free to modify and share!
