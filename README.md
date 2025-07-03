# Simple Qonto attachments downloader

Standard integrations in [Qonto](https://www.qonto.com) don’t allow you to export transactions attachments while preserving the original uploaded file names.
This simple script lets you fetch all attachments over a one-month period.

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
