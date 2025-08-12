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

if not all([LOGIN, SECRET, BANK_ACCOUNT_ID]):
    raise SystemExit(
        "Il faut définir QONTO_LOGIN, QONTO_SECRET et QONTO_BANK_ACCOUNT_ID"
    )

# Check if Google Drive is configured and available
USE_GOOGLE_DRIVE = GOOGLE_DRIVE_AVAILABLE and all(
    [GOOGLE_CREDENTIALS_PATH, GOOGLE_DRIVE_FOLDER_ID]
)

if USE_GOOGLE_DRIVE:
    print("Mode Google Drive activé")
elif not GOOGLE_DRIVE_AVAILABLE:
    print("Mode local activé (bibliothèques Google Drive non installées)")
else:
    print("Mode local activé (Google Drive non configuré)")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Télécharge tous les justificatifs d'une période donnée "
            "depuis l'API Qonto"
        )
    )
    parser.add_argument("--year", "-y", type=int, help="Année (ex : 2025)")
    parser.add_argument(
        "--month",
        "-m",
        type=int,
        choices=range(1, 13),
        help="Mois (1-12, ex : 6 pour juin)",
    )
    parser.add_argument(
        "--days",
        "-d",
        type=int,
        help="Synchroniser les N derniers jours (ex : 90 pour 3 mois)",
    )
    args = parser.parse_args()

    # Vérifier les combinaisons d'arguments
    if args.days and (args.year or args.month):
        parser.error("Utilisez soit --days, soit --year/--month, pas les deux.")
    elif bool(args.year) ^ bool(args.month):
        parser.error(
            "Il faut spécifier à la fois --year et --month, " "ou ni l'un ni l'autre."
        )
    return args


# CALCUL DE LA PÉRIODE
def compute_period(year_arg, month_arg, days_arg):
    today = date.today()

    if days_arg:
        # Mode "derniers N jours"
        end_date = today
        start_date = today - timedelta(days=days_arg)
        settled_from = start_date.isoformat() + "T00:00:00.000Z"
        settled_to = end_date.isoformat() + "T23:59:59.999Z"
        period_name = f"last_{days_arg}_days"
        return None, None, settled_from, settled_to, period_name
    elif year_arg and month_arg:
        # Mode mois spécifique
        year, month = year_arg, month_arg
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        settled_from = first_day.isoformat() + "T00:00:00.000Z"
        settled_to = last_day.isoformat() + "T23:59:59.999Z"
        period_name = f"receipts_{year}_{month:02d}"
        return year, month, settled_from, settled_to, period_name
    else:
        # Mode mois précédent par défaut
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
            f"Erreur: Le dossier parent Google Drive '{parent_folder_id}' "
            f"n'existe pas ou n'est pas accessible. Vérifiez:\n"
            f"1. Que l'ID du dossier est correct\n"
            f"2. Que le service account a accès au Shared Drive\n"
            f"3. Que le dossier n'a pas été supprimé\n"
            f"Détails: {e}"
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
        raise SystemExit(
            f"Erreur lors de la création/récupération du dossier '{folder_name}': {e}"
        )


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
        print(f"⚠️  Erreur lors de la recherche du fichier '{file_name}': {e}")
        print("   Création forcée du fichier...")
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
        print(f"❌ Erreur lors de l'upload du fichier '{file_name}': {e}")
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
        print(f"Erreur lors de la sauvegarde de l'état: {e}")


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
        print(f"Erreur lors de la sauvegarde de l'état: {e}")


def should_download_attachment(att, state):
    att_id = att["id"]
    if att_id not in state:
        return True

    stored = state[att_id]
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
        print(f"⚠️  Erreur lors du renommage '{old_filename}' → '{new_filename}': {e}")
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
    print(f"Période ciblée : {settled_from} → {settled_to}")

    # Setup storage (Google Drive or local)
    if USE_GOOGLE_DRIVE:
        # Initialize Google Drive service
        drive_service = get_drive_service()
        # Create or get the period folder in Google Drive (only for monthly mode)
        if args.days:
            # Mode "derniers jours" : pas de sous-dossier, tout dans le dossier parent
            period_folder_id = GOOGLE_DRIVE_FOLDER_ID
            storage_location = f"Google Drive root folder (derniers {args.days} jours)"
        else:
            # Mode mensuel : créer un sous-dossier
            period_folder_id = get_or_create_folder(
                drive_service, period_name, GOOGLE_DRIVE_FOLDER_ID
            )
            storage_location = f"Google Drive folder: {period_name}"
    else:
        # Use local storage
        drive_service = None
        if args.days:
            # Mode "derniers jours" : dossier unique
            local_output_dir = "receipts_sync"
            period_folder_id = None
        else:
            # Mode mensuel : dossier par mois
            local_output_dir = period_name
            period_folder_id = None
        os.makedirs(local_output_dir, exist_ok=True)
        storage_location = f"Local directory: {local_output_dir}"

    headers = {"Authorization": f"{LOGIN}:{SECRET}"}

    # Récupérer le cache des labels
    print("Chargement des labels...")
    labels_cache = get_labels_cache(headers)
    print(f"Labels chargés : {len(labels_cache)} labels trouvés")

    # Récupérer les transactions avec justificatifs
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

    print(f"Transactions trouvées avec justificatifs : {len(transactions)}")

    # Charger l'état des téléchargements précédents
    if USE_GOOGLE_DRIVE:
        # En mode "derniers jours", le state est à la racine du dossier parent
        state_folder_id = period_folder_id
        download_state = load_download_state(drive_service, state_folder_id)
    else:
        state_file_path = os.path.join(local_output_dir, ".download_state.json")
        download_state = load_download_state_local(state_file_path)

    # Télécharger chaque justificatif
    downloaded_count = 0
    skipped_count = 0

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
                    f"Téléchargement de « {original_filename} » → "
                    f"« {enriched_filename} » pour la transaction {tx_id}…"
                )
                file_data = requests.get(url).content

                # Déterminer le dossier de destination par mois
                if args.days:
                    # Mode "derniers jours" : organiser par mois
                    month_folder = get_month_folder_name(tx.get("settled_at", ""))
                    if USE_GOOGLE_DRIVE:
                        # Créer le dossier du mois dans Google Drive
                        month_folder_id = get_or_create_folder(
                            drive_service, month_folder, GOOGLE_DRIVE_FOLDER_ID
                        )
                        upload_file_to_drive(
                            drive_service, file_data, enriched_filename, month_folder_id
                        )
                    else:
                        # Créer le dossier du mois en local
                        month_dir = os.path.join("receipts_sync", month_folder)
                        os.makedirs(month_dir, exist_ok=True)
                        file_path = os.path.join(month_dir, enriched_filename)
                        upload_file_local(file_data, file_path)
                else:
                    # Mode mensuel : utiliser le dossier de la période
                    if USE_GOOGLE_DRIVE:
                        upload_file_to_drive(
                            drive_service,
                            file_data,
                            enriched_filename,
                            period_folder_id,
                        )
                    else:
                        file_path = os.path.join(local_output_dir, enriched_filename)
                        upload_file_local(file_data, file_path)
                update_attachment_state(att, enriched_filename, download_state)
                downloaded_count += 1
            elif should_rename_file(att, enriched_filename, download_state):
                # File exists but needs renaming due to label changes
                stored = download_state[att["id"]]
                old_filename = stored.get("enriched_file_name", "")

                # Déterminer le dossier pour le renommage
                if args.days:
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
                else:
                    if USE_GOOGLE_DRIVE:
                        renamed = rename_file_in_drive(
                            drive_service,
                            old_filename,
                            enriched_filename,
                            period_folder_id,
                        )
                    else:
                        old_file_path = os.path.join(local_output_dir, old_filename)
                        new_file_path = os.path.join(
                            local_output_dir, enriched_filename
                        )
                        renamed = rename_file_local(old_file_path, new_file_path)

                if renamed:
                    print(
                        f"Fichier renommé : « {old_filename} » → "
                        f"« {enriched_filename} »"
                    )
                    update_attachment_state(att, enriched_filename, download_state)
                    downloaded_count += 1  # Count as updated file
                else:
                    print(
                        f"Fichier « {old_filename} » non trouvé pour "
                        f"renommage, ignoré."
                    )
                    skipped_count += 1
            else:
                print(f"Fichier « {enriched_filename} » déjà téléchargé, ignoré.")
                skipped_count += 1

    # Sauvegarder l'état après tous les téléchargements
    if USE_GOOGLE_DRIVE:
        save_download_state(drive_service, state_folder_id, download_state)
    else:
        save_download_state_local(state_file_path, download_state)

    print(f"Téléchargements terminés dans {storage_location}")
    print(
        f"Fichiers téléchargés: {downloaded_count}, "
        f"fichiers ignorés: {skipped_count}"
    )


if __name__ == "__main__":
    main()
