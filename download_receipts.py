#!/usr/bin/env python3
import argparse
import calendar
import json
import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

LOGIN = os.getenv("QONTO_LOGIN")
SECRET = os.getenv("QONTO_SECRET")
BANK_ACCOUNT_ID = os.getenv("QONTO_BANK_ACCOUNT_ID")
API_URL = "https://thirdparty.qonto.com/v2"

if not all([LOGIN, SECRET, BANK_ACCOUNT_ID]):
    raise SystemExit(
        "Il faut définir QONTO_LOGIN, QONTO_SECRET et QONTO_BANK_ACCOUNT_ID"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Télécharge tous les justificatifs d'un mois donné "
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
    args = parser.parse_args()
    if bool(args.year) ^ bool(args.month):
        parser.error(
            "Il faut spécifier à la fois --year et --month, "
            "ou ni l'un ni l'autre."
        )
    return args


# CALCUL DE LA PÉRIODE
def compute_period(year_arg, month_arg):
    today = date.today()
    if year_arg and month_arg:
        year, month = year_arg, month_arg
    else:
        first_day_cur = today.replace(day=1)
        last_prev = first_day_cur - timedelta(days=1)
        year, month = last_prev.year, last_prev.month

    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    settled_from = first_day.isoformat() + "T00:00:00.000Z"
    settled_to = last_day.isoformat() + "T23:59:59.999Z"
    return year, month, settled_from, settled_to


def load_download_state(state_file):
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_download_state(state_file, state):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except IOError as e:
        print(f"Erreur lors de la sauvegarde de l'état: {e}")


def should_download_attachment(att, state):
    att_id = att["id"]
    if att_id not in state:
        return True

    stored = state[att_id]
    return stored.get("file_size") != att.get("file_size") or stored.get(
        "created_at"
    ) != att.get("created_at")


def update_attachment_state(att, state):
    att_id = att["id"]
    state[att_id] = {
        "file_name": att.get("file_name"),
        "file_size": att.get("file_size"),
        "created_at": att.get("created_at"),
        "file_content_type": att.get("file_content_type"),
    }


# MAIN
def main():
    args = parse_args()
    year, month, settled_from, settled_to = compute_period(
        args.year, args.month
    )
    print(f"Période ciblée : {settled_from} → {settled_to}")

    headers = {"Authorization": f"{LOGIN}:{SECRET}"}

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
        resp = requests.get(
            f"{API_URL}/transactions", headers=headers, params=params
        )
        resp.raise_for_status()
        data = resp.json()
        transactions.extend(data.get("transactions", []))
        if not data.get("meta", {}).get("next_page"):
            break
        page += 1

    print(f"Transactions trouvées avec justificatifs : {len(transactions)}")

    # Préparer le dossier de sortie
    output_dir = f"receipts_{year}_{month:02d}"
    os.makedirs(output_dir, exist_ok=True)

    # Charger l'état des téléchargements précédents
    state_file = f"{output_dir}/.download_state.json"
    download_state = load_download_state(state_file)

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
            file_name = att.get("file_name", f"{att['id']}.bin")
            url = att.get("url")
            if not url:
                continue

            if should_download_attachment(att, download_state):
                print(
                    f"Téléchargement de « {file_name} » "
                    f"pour la transaction {tx_id}…"
                )
                file_data = requests.get(url).content
                with open(os.path.join(output_dir, file_name), "wb") as f:
                    f.write(file_data)
                update_attachment_state(att, download_state)
                downloaded_count += 1
            else:
                print(f"Fichier « {file_name} » déjà téléchargé, ignoré.")
                skipped_count += 1

    # Sauvegarder l'état après tous les téléchargements
    save_download_state(state_file, download_state)

    print(f"Téléchargements terminés dans {output_dir}")
    print(
        f"Fichiers téléchargés: {downloaded_count}, "
        f"fichiers ignorés: {skipped_count}"
    )


if __name__ == "__main__":
    main()
