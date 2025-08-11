# Simple Qonto attachments downloader

Standard integrations in [Qonto](https://www.qonto.com) don't allow you to export transactions attachments while preserving the original uploaded file names.
This simple script lets you fetch all attachments over a one-month period.

The script avoids re-downloading already retrieved receipts thanks to a state tracking system. Only new files or those modified since the last download will be retrieved.

## with Docker

build

```
docker build -t qonto-receipts-downloader .
```

setup your env

```
cp .env.example .env
vim .env
```

use the script

```
docker run --rm --env-file .env -v "$(pwd)/output:/app" qonto-receipts-downloader --year 2025 --month 7
```

## Local Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure your environment variables in a `.env` file:
   ```
   QONTO_LOGIN=your_login
   QONTO_SECRET=your_api_secret
   QONTO_BANK_ACCOUNT_ID=your_bank_account_id
   ```

## Usage

```bash
# Download receipts from the previous month
python3 download_receipts.py

# Download receipts for a specific month
python3 download_receipts.py --year 2025 --month 7
```

## State System Operation

- **First run:** All files are downloaded
- **Subsequent runs:** Only new or modified files are downloaded
- **State file:** `.download_state.json` created in each period folder
- **Change detection:** Based on `file_size` and `created_at` from Qonto API

Already downloaded and unchanged files will be skipped, significantly speeding up subsequent synchronizations.

## Folder Structure

```
receipts_YYYY_MM/
├── receipt1.pdf
├── receipt2.pdf
└── .download_state.json  # State file (do not delete)
```
