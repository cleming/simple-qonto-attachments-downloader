#!/usr/bin/env python3
import argparse
import calendar
import io
import json
import os
import re
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

# Conditional imports for Google Drive
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

    GOOGLE_DRIVE_AVAILABLE = True
except ImportError:
    GOOGLE_DRIVE_AVAILABLE = False

load_dotenv()

LOGIN = os.getenv("QONTO_LOGIN")
SECRET = os.getenv("QONTO_SECRET")
BANK_ACCOUNT_ID = os.getenv("QONTO_BANK_ACCOUNT_ID")
API_URL = "https://thirdparty.qonto.com/v2"

# Google Drive configuration
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Slack configuration
SLACK_WEBHOOK_URL_ENV = os.getenv("SLACK_WEBHOOK_URL")
SLACK_MAX_LINES = int(os.getenv("SLACK_MAX_LINES", "30"))
SLACK_DEBUG = os.getenv("SLACK_DEBUG", "0") in ("1", "true", "True", "yes", "on")

if not all([LOGIN, SECRET, BANK_ACCOUNT_ID]):
    raise SystemExit(
        "QONTO_LOGIN, QONTO_SECRET and QONTO_BANK_ACCOUNT_ID must be defined"
    )

# Check if Google Drive is configured and available
USE_GOOGLE_DRIVE = GOOGLE_DRIVE_AVAILABLE and all(
    [GOOGLE_CREDENTIALS_PATH, GOOGLE_DRIVE_FOLDER_ID]
)

if USE_GOOGLE_DRIVE:
    print("Google Drive mode enabled")
elif not GOOGLE_DRIVE_AVAILABLE:
    print("Local mode enabled (Google Drive libraries not installed)")
else:
    print("Local mode enabled (Google Drive not configured)")


def parse_args():
    parser = argparse.ArgumentParser(
        description=("Download all receipts from a given period " "using the Qonto API")
    )
    parser.add_argument("--year", "-y", type=int, help="Year (e.g.: 2025)")
    parser.add_argument(
        "--month",
        "-m",
        type=int,
        choices=range(1, 13),
        help="Month (1-12, e.g.: 6 for June)",
    )
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        help="Sync last N days (e.g.: 90 for 3 months)",
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        help=(
            "Post a single Slack message when new invoices are added. "
            "Uses SLACK_WEBHOOK_URL unless --slack-webhook-url is provided."
        ),
    )
    parser.add_argument(
        "--slack-webhook-url",
        type=str,
        default=None,
        help="Slack Incoming Webhook URL (overrides SLACK_WEBHOOK_URL)",
    )
    args = parser.parse_args()

    # Check argument combinations
    if args.days and (args.year or args.month):
        parser.error("Use either --days or --year/--month, not both.")
    elif bool(args.year) ^ bool(args.month):
        parser.error("You must specify both --year and --month, " "or neither.")
    return args


# PERIOD CALCULATION
def compute_period(year_arg, month_arg, days_arg):
    today = date.today()

    if days_arg:
        # "Last N days" mode
        end_date = today
        start_date = today - timedelta(days=days_arg)
        settled_from = start_date.isoformat() + "T00:00:00.000Z"
        settled_to = end_date.isoformat() + "T23:59:59.999Z"
        period_name = f"last_{days_arg}_days"
        return None, None, settled_from, settled_to, period_name
    elif year_arg and month_arg:
        # Specific month mode
        year, month = year_arg, month_arg
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        settled_from = first_day.isoformat() + "T00:00:00.000Z"
        settled_to = last_day.isoformat() + "T23:59:59.999Z"
        period_name = f"receipts_{year}_{month:02d}"
        return year, month, settled_from, settled_to, period_name
    else:
        # Previous month mode by default
        first_day_cur = today.replace(day=1)
        last_prev = first_day_cur - timedelta(days=1)
        year, month = last_prev.year, last_prev.month
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        settled_from = first_day.isoformat() + "T00:00:00.000Z"
        settled_to = last_day.isoformat() + "T23:59:59.999Z"
        period_name = f"receipts_{year}_{month:02d}"
        return year, month, settled_from, settled_to, period_name


def clean_filename(filename):
    """Clean filename by removing/replacing invalid characters."""
    # Remove or replace characters not allowed in filenames
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    # Replace multiple spaces/underscores with single underscore
    filename = re.sub(r"[_\s]+", "_", filename)
    # Remove leading/trailing underscores
    filename = filename.strip("_")
    return filename


def create_enriched_filename(original_filename, tx, labels_cache=None, att_id=None):
    """Create enriched filename with amount, author, date, labels and unique ID."""
    # Extract file extension
    name, ext = os.path.splitext(original_filename)

    # Get transaction data
    amount = tx.get("amount", 0)
    author = tx.get("clean_counterparty_name") or tx.get("label", "Unknown")
    settled_at = tx.get("settled_at", "")
    tx_id = tx.get("id", "")

    # Parse and format date
    try:
        date_obj = datetime.fromisoformat(settled_at.replace("Z", "+00:00"))
        formatted_date = date_obj.strftime("%Y%m%d")
    except (ValueError, AttributeError):
        formatted_date = "unknown"

    # Format amount (remove decimals if .00)
    if amount == int(amount):
        amount_str = f"{int(amount)}EUR"
    else:
        amount_str = f"{amount:.2f}EUR"

    # Get labels
    label_names = []
    if labels_cache and tx.get("label_ids"):
        for label_id in tx["label_ids"]:
            if label_id in labels_cache:
                label_names.append(labels_cache[label_id])

    # Clean components
    name = clean_filename(name)
    author = clean_filename(author)
    labels_str = "_".join([clean_filename(label) for label in label_names])

    # Use attachment ID or first 8 chars of transaction ID for uniqueness
    unique_id = att_id or tx_id[:8] if tx_id else "unknown"

    # Create new filename: originalName-amount-author-date-labels-uniqueID.ext
    if labels_str:
        new_filename = f"{name}-{amount_str}-{author}-{formatted_date}-{labels_str}-{unique_id}{ext}"
    else:
        new_filename = f"{name}-{amount_str}-{author}-{formatted_date}-{unique_id}{ext}"

    return clean_filename(new_filename)


def get_month_folder_name(transaction_date):
    """Get folder name for a transaction based on its date."""
    try:
        # Parse the settled_at date
        date_obj = datetime.fromisoformat(transaction_date.replace("Z", "+00:00"))
        return f"{date_obj.year}-{date_obj.month:02d}"
    except (ValueError, AttributeError):
        return "unknown"


def get_labels_cache(headers):
    """Get all labels from Qonto API and return as dict."""
    labels_cache = {}
    page = 1

    while True:
        params = {"bank_account_id": BANK_ACCOUNT_ID, "page": page, "per_page": 100}
        resp = requests.get(f"{API_URL}/labels", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

        for label in data.get("labels", []):
            labels_cache[label["id"]] = label["name"]

        if not data.get("meta", {}).get("next_page"):
            break
        page += 1

    return labels_cache


def get_drive_service():
    """Initialize and return Google Drive service."""
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_PATH, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, folder_name, parent_folder_id):
    """Get or create a folder in Google Drive (supports Shared Drives)."""
    try:
        # First verify parent folder exists
        service.files().get(fileId=parent_folder_id, supportsAllDrives=True).execute()
    except Exception as e:
        raise SystemExit(
            f"Error: Google Drive parent folder '{parent_folder_id}' "
            f"does not exist or is not accessible. Check:\n"
            f"1. That the folder ID is correct\n"
            f"2. That the service account has access to the Shared Drive\n"
            f"3. That the folder has not been deleted\n"
            f"Details: {e}"
        )

    try:
        # Check if folder already exists
        escaped_name = escape_drive_query(folder_name)
        query = (
            f"name='{escaped_name}' and parents in '{parent_folder_id}' "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        results = (
            service.files()
            .list(
                q=query,
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        folders = results.get("files", [])

        if folders:
            return folders[0]["id"]

        # Create folder if it doesn't exist
        folder_metadata = {
            "name": folder_name,
            "parents": [parent_folder_id],
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = (
            service.files()
            .create(body=folder_metadata, fields="id", supportsAllDrives=True)
            .execute()
        )
        return folder.get("id")
    except Exception as e:
        raise SystemExit(f"Error creating/retrieving folder '{folder_name}': {e}")


def file_exists_in_drive(service, file_name, folder_id):
    """Check if a file exists in Google Drive folder."""
    escaped_name = escape_drive_query(file_name)
    query = f"name='{escaped_name}' and parents in '{folder_id}' " f"and trashed=false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    return len(files) > 0


def escape_drive_query(filename):
    """Escape filename for Google Drive query by doubling single quotes."""
    return filename.replace("'", "''")


def get_mimetype(file_name):
    """Get MIME type based on file extension."""
    extension = file_name.lower().split(".")[-1]
    mime_types = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return mime_types.get(extension, "application/octet-stream")


# Slack helpers
def _format_amount_eur(amount):
    try:
        if amount == int(amount):
            return f"{int(amount)}€"
        return f"{amount:.2f}€"
    except Exception:
        return f"{amount}€"


def post_to_slack(webhook_url, payload, fallback_text=None):
    try:
        if SLACK_DEBUG:
            print("[Slack] Payload preview:")
            try:
                print(json.dumps(payload, ensure_ascii=False)[:2000])
            except Exception:
                pass
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        # Log server message for better diagnosis
        print(f"⚠️  Slack responded {resp.status_code}: {resp.text}")
        # Fallback to plain text if provided and first attempt failed
        if fallback_text:
            fallback_payload = {"text": fallback_text}
            resp2 = requests.post(webhook_url, json=fallback_payload, timeout=10)
            if resp2.status_code == 200:
                print("Slack fallback (plain text) sent.")
                return True
            print(f"⚠️  Slack fallback failed {resp2.status_code}: {resp2.text}")
        return False
    except Exception as e:
        print(f"⚠️  Slack notification failed: {e}")
        return False


def build_slack_payload(new_items, period_label, drive_links=None):
    count = len(new_items)
    text = f"{count} nouvelles factures ajoutées {period_label}."

    # Build item lines with truncation to avoid Slack limits
    lines = []
    for item in new_items[:SLACK_MAX_LINES]:
        parts = []
        if item.get("date_str"):
            parts.append(item["date_str"])
        if item.get("author"):
            parts.append(item["author"])
        if item.get("amount") is not None:
            parts.append(_format_amount_eur(item["amount"]))
        summary = " · ".join(parts) if parts else item.get("filename", "(sans nom)")
        filename = item.get("filename", "")
        lines.append(f"• {summary} — {filename}")

    remaining = count - len(lines)
    if remaining > 0:
        lines.append(f"… et {remaining} autres")

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{count} nouvelles factures* ajoutées {period_label}",
            },
        },
    ]

    if lines:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        )

    if drive_links:
        # If multiple links, show them all; otherwise single link
        if isinstance(drive_links, dict):
            links_lines = [
                f"• <{url}|{month}>" for month, url in sorted(drive_links.items())
            ]
            links_text = "Dossiers Drive: " + " ".join(links_lines)
        else:
            links_text = f"Dossier Drive: <{drive_links}|ouvrir>"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": links_text}]})

    return {"text": text, "blocks": blocks}


def upload_file_to_drive(service, file_data, file_name, folder_id):
    """Upload or update a file to Google Drive (supports Shared Drives)."""
    try:
        # Check if file already exists
        escaped_name = escape_drive_query(file_name)
        query = (
            f"name='{escaped_name}' and parents in '{folder_id}' " f"and trashed=false"
        )
        results = (
            service.files()
            .list(
                q=query,
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])
    except Exception as e:
        print(f"⚠️  Error searching for file '{file_name}': {e}")
        print("   Forcing file creation...")
        files = []  # Force creation

    mimetype = get_mimetype(file_name)
    media = MediaIoBaseUpload(io.BytesIO(file_data), mimetype=mimetype, resumable=True)

    try:
        if files:
            # Update existing file
            file_id = files[0]["id"]
            service.files().update(
                fileId=file_id, media_body=media, supportsAllDrives=True
            ).execute()
        else:
            # Create new file
            file_metadata = {"name": file_name, "parents": [folder_id]}
            service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
    except Exception as e:
        print(f"❌ Error uploading file '{file_name}': {e}")
        raise


def download_file_from_drive(service, file_name, folder_id):
    """Download a file from Google Drive (supports Shared Drives)."""
    escaped_name = escape_drive_query(file_name)
    query = f"name='{escaped_name}' and parents in '{folder_id}' " f"and trashed=false"
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = results.get("files", [])

    if not files:
        return None

    file_id = files[0]["id"]
    request = service.files().get_media(fileId=file_id)
    file_io = io.BytesIO()
    downloader = MediaIoBaseDownload(file_io, request)

    done = False
    while done is False:
        status, done = downloader.next_chunk()

    return file_io.getvalue()


# Local storage functions
def load_download_state_local(state_file_path):
    """Load download state from local file."""
    if os.path.exists(state_file_path):
        try:
            with open(state_file_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_download_state_local(state_file_path, state):
    """Save download state to local file."""
    try:
        with open(state_file_path, "w") as f:
            json.dump(state, f, indent=2)
    except IOError as e:
        print(f"Error saving state: {e}")


def upload_file_local(file_data, file_path):
    """Save file to local filesystem."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(file_data)


# Google Drive functions
def load_download_state(service, folder_id):
    """Load download state from Google Drive."""
    state_data = download_file_from_drive(service, ".download_state.json", folder_id)
    if state_data:
        try:
            return json.loads(state_data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return {}


def save_download_state(service, folder_id, state):
    """Save download state to Google Drive."""
    try:
        state_json = json.dumps(state, indent=2)
        upload_file_to_drive(
            service, state_json.encode("utf-8"), ".download_state.json", folder_id
        )
    except Exception as e:
        print(f"Error saving state: {e}")


def should_download_attachment(att, state):
    att_id = att["id"]
    if att_id not in state:
        return True

    stored = state[att_id]

    # For Qonto invoices, always re-download if the filename suggests content changed
    original_filename = att.get("file_name", "")
    if "invoice-" in original_filename:
        # Check if any key metadata changed (size, creation date, or filename)
        return (
            stored.get("file_size") != att.get("file_size")
            or stored.get("created_at") != att.get("created_at")
            or stored.get("original_file_name") != original_filename
        )

    # For other files, use the original logic
    return stored.get("file_size") != att.get("file_size") or stored.get(
        "created_at"
    ) != att.get("created_at")


def should_rename_file(att, enriched_filename, state):
    """Check if file should be renamed due to label changes."""
    att_id = att["id"]
    if att_id not in state:
        return False

    stored = state[att_id]
    stored_filename = stored.get("enriched_file_name", "")

    # Don't rename Qonto invoices as they are updated over time
    original_filename = att.get("file_name", "")
    if "invoice-" in original_filename and "Qonto" in enriched_filename:
        return False

    # File content hasn't changed but filename is different
    return (
        stored.get("file_size") == att.get("file_size")
        and stored.get("created_at") == att.get("created_at")
        and stored_filename != enriched_filename
        and stored_filename != ""  # Make sure we have a previous filename
    )


def rename_file_in_drive(service, old_filename, new_filename, folder_id):
    """Rename a file in Google Drive (supports Shared Drives)."""
    try:
        escaped_name = escape_drive_query(old_filename)
        query = (
            f"name='{escaped_name}' and parents in '{folder_id}' " f"and trashed=false"
        )
        results = (
            service.files()
            .list(
                q=query,
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])

        if files:
            file_id = files[0]["id"]
            body = {"name": new_filename}
            service.files().update(
                fileId=file_id, body=body, supportsAllDrives=True
            ).execute()
            return True
        return False
    except Exception as e:
        print(f"⚠️  Error renaming '{old_filename}' → '{new_filename}': {e}")
        return False


def rename_file_local(old_file_path, new_file_path):
    """Rename a file locally."""
    if os.path.exists(old_file_path):
        os.rename(old_file_path, new_file_path)
        return True
    return False


def update_attachment_state(att, enriched_filename, state):
    att_id = att["id"]
    state[att_id] = {
        "original_file_name": att.get("file_name"),
        "enriched_file_name": enriched_filename,
        "file_size": att.get("file_size"),
        "created_at": att.get("created_at"),
        "file_content_type": att.get("file_content_type"),
    }


# MAIN
def main():
    args = parse_args()
    year, month, settled_from, settled_to, period_name = compute_period(
        args.year, args.month, args.days
    )
    print(f"Target period: {settled_from} → {settled_to}")

    # Setup storage (Google Drive or local)
    # Always organize files by month, regardless of the flag used
    if USE_GOOGLE_DRIVE:
        # Initialize Google Drive service
        drive_service = get_drive_service()
        # Use parent folder directly, files will be organized by month
        period_folder_id = GOOGLE_DRIVE_FOLDER_ID
        if args.days:
            storage_location = f"Google Drive root folder (last {args.days} days)"
        else:
            storage_location = f"Google Drive root folder ({year}-{month:02d})"
    else:
        # Use local storage with single root folder
        drive_service = None
        local_output_dir = "receipts_sync"
        period_folder_id = None
        os.makedirs(local_output_dir, exist_ok=True)
        if args.days:
            storage_location = f"Local directory: {local_output_dir} (last {args.days} days)"
        else:
            storage_location = f"Local directory: {local_output_dir} ({year}-{month:02d})"

    headers = {"Authorization": f"{LOGIN}:{SECRET}"}

    # Récupérer le cache des labels
    print("Loading labels...")
    labels_cache = get_labels_cache(headers)
    print(f"Labels loaded: {len(labels_cache)} labels found")

    # Fetch transactions with attachments
    transactions = []
    page, per_page = 1, 100
    while True:
        params = {
            "bank_account_id": BANK_ACCOUNT_ID,
            "with_attachments": "true",
            "settled_at_from": settled_from,
            "settled_at_to": settled_to,
            "per_page": per_page,
            "page": page,
            "sort_by": "settled_at:asc",
        }
        resp = requests.get(f"{API_URL}/transactions", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        transactions.extend(data.get("transactions", []))
        if not data.get("meta", {}).get("next_page"):
            break
        page += 1

    print(f"Transactions found with attachments: {len(transactions)}")

    # Load previous download state (always at root level)
    if USE_GOOGLE_DRIVE:
        # State is always at the root of parent folder
        state_folder_id = GOOGLE_DRIVE_FOLDER_ID
        download_state = load_download_state(drive_service, state_folder_id)
    else:
        state_file_path = os.path.join(local_output_dir, ".download_state.json")
        download_state = load_download_state_local(state_file_path)

    # Download each attachment
    downloaded_count = 0
    skipped_count = 0
    new_items = []  # For Slack summary
    drive_month_folders = {}  # month -> folder_id (Drive only)

    for tx in transactions:
        tx_id = tx["id"]
        att_resp = requests.get(
            f"{API_URL}/transactions/{tx_id}/attachments", headers=headers
        )
        att_resp.raise_for_status()
        for att in att_resp.json().get("attachments", []):
            original_filename = att.get("file_name", f"{att['id']}.bin")
            enriched_filename = create_enriched_filename(
                original_filename, tx, labels_cache, att["id"]
            )
            url = att.get("url")
            if not url:
                continue

            if should_download_attachment(att, download_state):
                print(
                    f"Downloading '{original_filename}' → "
                    f"'{enriched_filename}' for transaction {tx_id}..."
                )
                file_data = requests.get(url).content

                # Always organize by month, regardless of the flag used
                month_folder = get_month_folder_name(tx.get("settled_at", ""))
                if USE_GOOGLE_DRIVE:
                    # Create month folder in Google Drive
                    month_folder_id = get_or_create_folder(
                        drive_service, month_folder, GOOGLE_DRIVE_FOLDER_ID
                    )
                    drive_month_folders[month_folder] = month_folder_id
                    upload_file_to_drive(
                        drive_service, file_data, enriched_filename, month_folder_id
                    )
                else:
                    # Create month folder locally
                    month_dir = os.path.join("receipts_sync", month_folder)
                    os.makedirs(month_dir, exist_ok=True)
                    file_path = os.path.join(month_dir, enriched_filename)
                    upload_file_local(file_data, file_path)
                update_attachment_state(att, enriched_filename, download_state)
                downloaded_count += 1
                # Track for Slack
                try:
                    # Prepare human date
                    settled_at = tx.get("settled_at", "")
                    date_obj = (
                        datetime.fromisoformat(settled_at.replace("Z", "+00:00"))
                        if settled_at
                        else None
                    )
                    date_str = date_obj.strftime("%Y-%m-%d") if date_obj else None
                except Exception:
                    date_str = None
                new_items.append(
                    {
                        "filename": enriched_filename,
                        "amount": tx.get("amount"),
                        "author": tx.get("clean_counterparty_name")
                        or tx.get("label", "Unknown"),
                        "date_str": date_str,
                        "month": month_folder,
                    }
                )
            elif should_rename_file(att, enriched_filename, download_state):
                # File exists but needs renaming due to label changes
                stored = download_state[att["id"]]
                old_filename = stored.get("enriched_file_name", "")

                # Always organize by month for renaming too
                month_folder = get_month_folder_name(tx.get("settled_at", ""))
                if USE_GOOGLE_DRIVE:
                    month_folder_id = get_or_create_folder(
                        drive_service, month_folder, GOOGLE_DRIVE_FOLDER_ID
                    )
                    renamed = rename_file_in_drive(
                        drive_service,
                        old_filename,
                        enriched_filename,
                        month_folder_id,
                    )
                else:
                    month_dir = os.path.join("receipts_sync", month_folder)
                    old_file_path = os.path.join(month_dir, old_filename)
                    new_file_path = os.path.join(month_dir, enriched_filename)
                    renamed = rename_file_local(old_file_path, new_file_path)

                if renamed:
                    print(f"File renamed: '{old_filename}' → " f"'{enriched_filename}'")
                    update_attachment_state(att, enriched_filename, download_state)
                    downloaded_count += 1  # Count as updated file
                else:
                    print(f"File '{old_filename}' not found for " f"renaming, skipped.")
                    skipped_count += 1
            else:
                print(f"File '{enriched_filename}' already downloaded, skipped.")
                skipped_count += 1

    # Save state after all downloads
    if USE_GOOGLE_DRIVE:
        save_download_state(drive_service, state_folder_id, download_state)
    else:
        save_download_state_local(state_file_path, download_state)

    print(f"Downloads completed in {storage_location}")
    print(f"Files downloaded: {downloaded_count}, " f"files skipped: {skipped_count}")

    # Optional Slack notification
    if args.slack:
        webhook_url = args.slack_webhook_url or SLACK_WEBHOOK_URL_ENV
        if not webhook_url:
            print(
                "⚠️  --slack enabled but no webhook URL provided. "
                "Set SLACK_WEBHOOK_URL or pass --slack-webhook-url."
            )
        elif new_items:
            if USE_GOOGLE_DRIVE:
                # If a single month, link that folder; else show each
                months = sorted(set(item["month"] for item in new_items if item.get("month")))
                if len(months) == 1 and months[0] in drive_month_folders:
                    drive_links = f"https://drive.google.com/drive/folders/{drive_month_folders[months[0]]}"
                else:
                    # Map month -> link when known; fallback to parent folder link
                    links = {}
                    for m in months:
                        if m in drive_month_folders:
                            links[m] = f"https://drive.google.com/drive/folders/{drive_month_folders[m]}"
                    if not links:
                        links = f"https://drive.google.com/drive/folders/{GOOGLE_DRIVE_FOLDER_ID}"
                    drive_links = links
            else:
                drive_links = None

            # Build a human label for period
            if args.days:
                period_label = f"(sur les {args.days} derniers jours)"
            elif year and month:
                period_label = f"pour {year}-{month:02d}"
            else:
                period_label = "pour la période demandée"

            payload = build_slack_payload(new_items, period_label, drive_links)
            # Build a concise plain-text fallback
            try:
                first_lines = []
                for it in new_items[:SLACK_MAX_LINES]:
                    bits = []
                    if it.get("date_str"):
                        bits.append(it["date_str"])
                    if it.get("author"):
                        bits.append(it["author"])
                    if it.get("amount") is not None:
                        bits.append(_format_amount_eur(it["amount"]))
                    first_lines.append(" - " + " | ".join(bits))
                rem = len(new_items) - len(first_lines)
                if rem > 0:
                    first_lines.append(f" ... (+{rem} autres)")
                plain = (
                    f"{len(new_items)} nouvelles factures {period_label}.\n" + "\n".join(first_lines)
                )
            except Exception:
                plain = f"{len(new_items)} nouvelles factures {period_label}."
            ok = post_to_slack(webhook_url, payload, fallback_text=plain)
            if ok:
                print("Slack notification sent.")


if __name__ == "__main__":
    main()
