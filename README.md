# Simple Qonto attachments downloader

Standard integrations in [Qonto](https://www.qonto.com) don't allow you to export transactions attachments while preserving the original uploaded file names.
This simple script lets you fetch all attachments over a one-month period.

The script avoids re-downloading already retrieved receipts thanks to a state tracking system. Only new files or those modified since the last download will be retrieved.

**Flexible Storage:** The script supports both Google Drive (for serverless environments like AWS Lambda) and local file storage (for simpler setups).

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
   # For local storage only
   pip install python-dotenv requests
   
   # For Google Drive support (optional)
   pip install -r requirements.txt
   ```
3. Configure your environment variables in a `.env` file:
   ```bash
   # Required for all modes
   QONTO_LOGIN=your_login
   QONTO_SECRET=your_api_secret
   QONTO_BANK_ACCOUNT_ID=your_bank_account_id
   
   # Optional for Google Drive mode
   GOOGLE_CREDENTIALS_PATH=path/to/your/service-account.json
   GOOGLE_DRIVE_FOLDER_ID=your_drive_folder_id
   ```

## Storage Modes

The script automatically detects the storage mode:

- **Local Mode**: If Google Drive variables are not set or libraries not installed
  - Files stored in `receipts_YYYY_MM/` directory
  - State file: `receipts_YYYY_MM/.download_state.json`
  - No additional setup required

- **Google Drive Mode**: If Google Drive is properly configured
  - Files stored on Google Drive
  - Perfect for serverless deployments
  - Requires Google Drive setup (see below)

## Google Drive Setup

1. **Create a Google Cloud Project:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project or select existing one

2. **Enable Google Drive API:**
   - In the Google Cloud Console, navigate to "APIs & Services" > "Library"
   - Search for "Google Drive API" and enable it

3. **Create a Service Account:**
   - Go to "APIs & Services" > "Credentials"
   - Click "Create Credentials" > "Service Account"
   - Download the JSON credentials file

4. **Share Google Drive Folder:**
   - Create a folder in Google Drive where receipts will be stored
   - Share this folder with the service account email (from the JSON file)
   - Copy the folder ID from the URL (the long string after `/folders/`)

5. **Set Environment Variables:**
   - `GOOGLE_CREDENTIALS_PATH`: Path to your service account JSON file
   - `GOOGLE_DRIVE_FOLDER_ID`: The folder ID where receipts will be stored

## Usage

```bash
# Download receipts from the previous month
python3 download_receipts.py

# Download receipts for a specific month
python3 download_receipts.py --year 2025 --month 7

# Post a Slack summary if new invoices were added (uses SLACK_WEBHOOK_URL)
python3 download_receipts.py --year 2025 --month 7 --slack

# Or pass the webhook explicitly
python3 download_receipts.py --year 2025 --month 7 --slack --slack-webhook-url https://hooks.slack.com/services/XXX/YYY/ZZZ
```

## State System Operation

- **First run:** All files are downloaded to chosen storage
- **Subsequent runs:** Only new or modified files are downloaded
- **State file:** `.download_state.json` stored in each period folder
- **Change detection:** Based on `file_size` and `created_at` from Qonto API

Already downloaded and unchanged files will be skipped, significantly speeding up subsequent synchronizations.

## Folder Structure

### Local Mode
```
receipts_YYYY_MM/
├── receipt1-222EUR-Trainline-20250812-Tournée.pdf
├── receipt2-150EUR-Restaurant-20250813.pdf
└── .download_state.json  # State file (do not delete)
```

### Google Drive Mode
```
Your Drive Folder/
└── receipts_YYYY_MM/
    ├── receipt1-222EUR-Trainline-20250812-Tournée.pdf
    ├── receipt2-150EUR-Restaurant-20250813.pdf
    └── .download_state.json  # State file (do not delete)
```

## AWS Lambda Deployment

This script is designed to work perfectly in AWS Lambda:

1. Package the script with dependencies
2. Set environment variables in Lambda configuration
3. Upload the Google Service Account JSON as a Lambda layer or embed it
4. Set up CloudWatch Events for scheduled runs

The stateless nature of Lambda is handled by storing all state in Google Drive.

## Slack Notifications (Optional)

- Set `SLACK_WEBHOOK_URL` in your environment or `.env` file.
- Run with `--slack` to send one message when new invoices are added.
- The message summarizes newly added files (date, merchant, amount) and, in Google Drive mode, includes a link to the relevant folder(s).

### Troubleshooting 400 Bad Request

- Ensure the webhook URL is correct and active for your Slack workspace.
- Some Slack apps have stricter payload rules; this script sends Block Kit with a `text` fallback. On 400 responses, it automatically retries with a plain-text fallback.
- Very long summaries can exceed Slack limits; the script truncates to `SLACK_MAX_LINES` (default 30). You can adjust via env var.
- Enable debug to inspect payload: set `SLACK_DEBUG=1` to print a preview in the console.
