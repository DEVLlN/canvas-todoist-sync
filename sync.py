#!/usr/bin/env python3
"""
Canvas to Todoist Sync

Synchronizes Canvas LMS assignments from an ICS calendar feed to Todoist tasks.
Handles deduplication, updates, and organizes tasks with labels by course.

Runs hourly via GitHub Actions.
"""

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from icalendar import Calendar
from todoist_api_python.api import TodoistAPI

# Reminder settings
REMINDER_DAYS_BEFORE = int(os.environ.get("REMINDER_DAYS_BEFORE", "1"))  # Days before due date

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Configuration
CANVAS_ICS_URL = os.environ.get("CANVAS_ICS_URL", "")
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")
PROJECT_NAME = os.environ.get("TODOIST_PROJECT_NAME", "Canvas Assignments")
STATE_FILE = os.environ.get("STATE_FILE", "sync_state.json")

# Priority mapping based on days until due
# Todoist priorities: 4 = urgent (red), 3 = high (orange), 2 = medium (yellow), 1 = normal
PRIORITY_THRESHOLDS = {
    1: 4,   # Due within 1 day -> urgent
    3: 3,   # Due within 3 days -> high
    7: 2,   # Due within 7 days -> medium
}
DEFAULT_PRIORITY = 1  # Normal priority for assignments due later


class SyncState:
    """Manages persistent state for tracking synced assignments."""

    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self.state = self._load()

    def _load(self) -> dict:
        """Load state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Could not load state file: {e}. Starting fresh.")
        return {"synced_events": {}, "last_sync": None}

    def save(self):
        """Save state to file."""
        self.state["last_sync"] = datetime.now(timezone.utc).isoformat()
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)
        logger.info(f"State saved to {self.state_file}")

    def get_synced_event(self, event_uid: str) -> Optional[dict]:
        """Get info about a previously synced event."""
        return self.state["synced_events"].get(event_uid)

    def mark_synced(self, event_uid: str, todoist_task_id: str, event_hash: str, due_date: str = None):
        """Mark an event as synced."""
        self.state["synced_events"][event_uid] = {
            "todoist_task_id": todoist_task_id,
            "event_hash": event_hash,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "due_date": due_date,
        }

    def mark_completed(self, event_uid: str):
        """Mark an event as auto-completed (remove from tracking)."""
        if event_uid in self.state["synced_events"]:
            del self.state["synced_events"][event_uid]

    def get_all_synced_uids(self) -> set:
        """Get all synced event UIDs."""
        return set(self.state["synced_events"].keys())


def fetch_ics_feed(url: str) -> str:
    """Fetch the ICS calendar feed from Canvas."""
    logger.info(f"Fetching ICS feed from Canvas...")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        logger.info(f"Successfully fetched ICS feed ({len(response.text)} bytes)")
        return response.text
    except requests.RequestException as e:
        logger.error(f"Failed to fetch ICS feed: {e}")
        raise


def parse_course_name(summary: str, description: str = "") -> str:
    """Extract course name from event summary or description."""
    # Canvas typically formats as: "Assignment Name [Course Name]"
    # or includes course info in the description

    bracket_match = re.search(r'\[([^\]]+)\]', summary)
    if bracket_match:
        return bracket_match.group(1).strip()

    # Try to find course pattern in description (e.g., "CHEM 350")
    course_pattern = re.search(r'([A-Z]{2,4}\s*\d{3}[A-Z]?)', summary + " " + description)
    if course_pattern:
        return course_pattern.group(1).strip()

    # Fallback: use first part before colon or dash
    for sep in [':', ' - ', ' – ']:
        if sep in summary:
            return summary.split(sep)[0].strip()

    return "General"


def parse_assignment_title(summary: str) -> str:
    """Clean up assignment title, removing course brackets."""
    # Remove [Course Name] suffix if present
    title = re.sub(r'\s*\[[^\]]+\]\s*$', '', summary)
    return title.strip()


def compute_event_hash(event: dict) -> str:
    """Compute a hash of event details for change detection."""
    hash_content = f"{event['summary']}|{event['due_date']}|{event['description']}"
    return hashlib.md5(hash_content.encode()).hexdigest()


def calculate_priority(due_date: datetime) -> int:
    """Calculate Todoist priority based on days until due."""
    now = datetime.now(timezone.utc)

    # Handle naive datetimes
    if due_date.tzinfo is None:
        due_date = due_date.replace(tzinfo=timezone.utc)

    days_until_due = (due_date - now).days

    for threshold_days, priority in sorted(PRIORITY_THRESHOLDS.items()):
        if days_until_due <= threshold_days:
            return priority

    return DEFAULT_PRIORITY


def parse_ics_events(ics_content: str) -> list[dict]:
    """Parse ICS content and extract assignment events."""
    calendar = Calendar.from_ical(ics_content)
    events = []

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        # Extract event details
        uid = str(component.get("uid", ""))
        summary = str(component.get("summary", ""))
        description = str(component.get("description", ""))

        # Get due date (DTEND or DTSTART)
        dt = component.get("dtend") or component.get("dtstart")
        if dt is None:
            logger.warning(f"Skipping event without date: {summary}")
            continue

        due_date = dt.dt
        # Convert date to datetime if needed
        if not isinstance(due_date, datetime):
            due_date = datetime.combine(due_date, datetime.min.time(), tzinfo=timezone.utc)
        elif due_date.tzinfo is None:
            due_date = due_date.replace(tzinfo=timezone.utc)

        # Skip past events
        if due_date < datetime.now(timezone.utc):
            logger.debug(f"Skipping past event: {summary}")
            continue

        course_name = parse_course_name(summary, description)
        title = parse_assignment_title(summary)

        event = {
            "uid": uid,
            "summary": summary,
            "title": title,
            "description": description,
            "due_date": due_date.isoformat(),
            "due_datetime": due_date,
            "course": course_name,
            "priority": calculate_priority(due_date),
        }
        events.append(event)

    logger.info(f"Parsed {len(events)} upcoming events from ICS feed")
    return events


def sanitize_label_name(name: str) -> str:
    """Sanitize a string to be a valid Todoist label name."""
    # Remove special characters, replace spaces with underscores
    sanitized = re.sub(r'[^\w\s-]', '', name)
    sanitized = re.sub(r'\s+', '_', sanitized)
    return sanitized.strip('_')


class TodoistSync:
    """Handles Todoist API operations for syncing."""

    def __init__(self, api_token: str):
        self.api = TodoistAPI(api_token)
        self.api_token = api_token
        self._projects_cache = None
        self._labels_cache = None

    def _get_all_projects(self) -> list:
        """Get all projects, handling paginator responses."""
        result = self.api.get_projects()
        # The paginator returns pages (lists of projects), flatten them
        all_projects = []
        for page in result:
            if isinstance(page, list):
                all_projects.extend(page)
            else:
                all_projects.append(page)
        return all_projects

    def _get_all_labels(self) -> list:
        """Get all labels, handling paginator responses."""
        result = self.api.get_labels()
        # The paginator returns pages (lists of labels), flatten them
        all_labels = []
        for page in result:
            if isinstance(page, list):
                all_labels.extend(page)
            else:
                all_labels.append(page)
        return all_labels

    def get_or_create_project(self, name: str) -> str:
        """Get existing project or create new one. Returns project ID."""
        if self._projects_cache is None:
            projects = self._get_all_projects()
            self._projects_cache = {p.name: p.id for p in projects}

        if name in self._projects_cache:
            logger.info(f"Using existing project: {name}")
            return self._projects_cache[name]

        logger.info(f"Creating new project: {name}")
        project = self.api.add_project(name=name)
        self._projects_cache[name] = project.id
        return project.id

    def get_or_create_label(self, name: str) -> str:
        """Get existing label or create new one. Returns label name."""
        sanitized_name = sanitize_label_name(name)

        if self._labels_cache is None:
            labels = self._get_all_labels()
            self._labels_cache = {l.name: l.id for l in labels}

        if sanitized_name in self._labels_cache:
            return sanitized_name

        logger.info(f"Creating new label: {sanitized_name}")
        try:
            label = self.api.add_label(name=sanitized_name)
            self._labels_cache[label.name] = label.id
            return label.name
        except Exception as e:
            logger.warning(f"Could not create label {sanitized_name}: {e}")
            return sanitized_name

    def create_task(
        self,
        title: str,
        project_id: str,
        due_datetime: datetime,
        description: str = "",
        labels: list[str] = None,
        priority: int = 1,
    ) -> str:
        """Create a new Todoist task. Returns task ID."""
        # Format due string for Todoist
        due_string = due_datetime.strftime("%Y-%m-%d at %H:%M")

        task = self.api.add_task(
            content=title,
            project_id=project_id,
            description=description[:16383] if description else "",  # Todoist limit
            due_string=due_string,
            labels=labels or [],
            priority=priority,
        )

        logger.info(f"Created task: {title} (ID: {task.id})")
        return task.id

    def update_task(
        self,
        task_id: str,
        title: str = None,
        due_datetime: datetime = None,
        description: str = None,
        priority: int = None,
    ):
        """Update an existing Todoist task."""
        kwargs = {}
        if title:
            kwargs["content"] = title
        if due_datetime:
            kwargs["due_string"] = due_datetime.strftime("%Y-%m-%d at %H:%M")
        if description is not None:
            kwargs["description"] = description[:16383]
        if priority:
            kwargs["priority"] = priority

        if kwargs:
            self.api.update_task(task_id=task_id, **kwargs)
            logger.info(f"Updated task: {task_id}")

    def task_exists(self, task_id: str) -> bool:
        """Check if a task still exists (not deleted/completed)."""
        try:
            self.api.get_task(task_id=task_id)
            return True
        except Exception:
            return False

    def complete_task(self, task_id: str) -> bool:
        """Mark a task as complete. Returns True if successful."""
        try:
            self.api.close_task(task_id=task_id)
            logger.info(f"Completed task: {task_id}")
            return True
        except Exception as e:
            logger.warning(f"Could not complete task {task_id}: {e}")
            return False

    def add_reminder(self, task_id: str, remind_at: datetime) -> bool:
        """Add a reminder to a task using the Sync API."""
        try:
            # Use Todoist Sync API for reminders (REST API doesn't support them)
            import uuid
            temp_id = str(uuid.uuid4())

            # Format the reminder time
            remind_str = remind_at.strftime("%Y-%m-%dT%H:%M:%S")

            response = requests.post(
                "https://api.todoist.com/sync/v9/sync",
                headers={"Authorization": f"Bearer {self.api_token}"},
                json={
                    "commands": [
                        {
                            "type": "reminder_add",
                            "temp_id": temp_id,
                            "uuid": str(uuid.uuid4()),
                            "args": {
                                "item_id": task_id,
                                "due": {"date": remind_str},
                            },
                        }
                    ]
                },
                timeout=30,
            )
            response.raise_for_status()
            logger.info(f"Added reminder for task {task_id} at {remind_str}")
            return True
        except Exception as e:
            logger.warning(f"Could not add reminder for task {task_id}: {e}")
            return False


def sync_canvas_to_todoist():
    """Main sync function."""
    logger.info("=" * 50)
    logger.info("Starting Canvas to Todoist sync")
    logger.info("=" * 50)

    # Validate configuration
    if not TODOIST_API_TOKEN:
        logger.error("TODOIST_API_TOKEN environment variable is not set")
        sys.exit(1)

    if not CANVAS_ICS_URL:
        logger.error("CANVAS_ICS_URL environment variable is not set")
        sys.exit(1)

    # Initialize components
    state = SyncState(STATE_FILE)
    todoist = TodoistSync(TODOIST_API_TOKEN)

    # Fetch and parse ICS feed
    try:
        ics_content = fetch_ics_feed(CANVAS_ICS_URL)
        events = parse_ics_events(ics_content)
    except Exception as e:
        logger.error(f"Failed to fetch/parse ICS feed: {e}")
        sys.exit(1)

    if not events:
        logger.info("No upcoming events found in ICS feed")
        state.save()
        return

    # Get or create the Canvas project
    project_id = todoist.get_or_create_project(PROJECT_NAME)

    # Process each event
    stats = {"created": 0, "updated": 0, "skipped": 0, "completed": 0}

    # Get current event UIDs for auto-complete detection
    current_event_uids = {event["uid"] for event in events}

    # Check for assignments that disappeared (likely submitted)
    for event_uid in list(state.get_all_synced_uids()):
        if event_uid not in current_event_uids:
            synced_info = state.get_synced_event(event_uid)
            if synced_info and synced_info.get("due_date"):
                try:
                    due_date = datetime.fromisoformat(synced_info["due_date"])
                    # If due date is still in the future, assignment was likely submitted
                    if due_date > datetime.now(timezone.utc):
                        task_id = synced_info["todoist_task_id"]
                        if todoist.task_exists(task_id):
                            logger.info(f"Assignment disappeared from Canvas (likely submitted), completing task: {task_id}")
                            if todoist.complete_task(task_id):
                                stats["completed"] += 1
                        state.mark_completed(event_uid)
                except (ValueError, TypeError) as e:
                    logger.debug(f"Could not parse due date for {event_uid}: {e}")

    for event in events:
        event_uid = event["uid"]
        event_hash = compute_event_hash(event)

        # Check if already synced
        synced_info = state.get_synced_event(event_uid)

        if synced_info:
            # Check if event has changed
            if synced_info["event_hash"] == event_hash:
                logger.debug(f"Skipping unchanged event: {event['title']}")
                stats["skipped"] += 1
                continue

            # Check if task still exists
            if todoist.task_exists(synced_info["todoist_task_id"]):
                # Update existing task
                logger.info(f"Updating changed event: {event['title']}")
                todoist.update_task(
                    task_id=synced_info["todoist_task_id"],
                    title=event["title"],
                    due_datetime=event["due_datetime"],
                    description=event["description"],
                    priority=event["priority"],
                )
                state.mark_synced(event_uid, synced_info["todoist_task_id"], event_hash, event["due_date"])
                stats["updated"] += 1
                continue

        # Create new task
        logger.info(f"Creating new task for: {event['title']}")

        # Get or create label for course
        course_label = todoist.get_or_create_label(event["course"])

        try:
            task_id = todoist.create_task(
                title=event["title"],
                project_id=project_id,
                due_datetime=event["due_datetime"],
                description=event["description"],
                labels=[course_label],
                priority=event["priority"],
            )

            # Add reminder for 1 day before due date
            if REMINDER_DAYS_BEFORE > 0:
                reminder_time = event["due_datetime"] - timedelta(days=REMINDER_DAYS_BEFORE)
                # Only add reminder if it's in the future
                if reminder_time > datetime.now(timezone.utc):
                    todoist.add_reminder(task_id, reminder_time)

            state.mark_synced(event_uid, task_id, event_hash, event["due_date"])
            stats["created"] += 1
        except Exception as e:
            logger.error(f"Failed to create task for {event['title']}: {e}")

    # Save state
    state.save()

    # Summary
    logger.info("=" * 50)
    logger.info("Sync complete!")
    logger.info(f"  Created: {stats['created']}")
    logger.info(f"  Updated: {stats['updated']}")
    logger.info(f"  Skipped: {stats['skipped']}")
    logger.info(f"  Auto-completed: {stats['completed']}")
    logger.info("=" * 50)


if __name__ == "__main__":
    sync_canvas_to_todoist()
