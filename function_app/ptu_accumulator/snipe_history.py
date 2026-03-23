"""
Snipe history -- stores and retrieves attempt logs in Azure Blob Storage.
Each cycle writes a JSON entry. Dashboard reads the last N entries.
"""
import os
import json
import datetime
import logging


# Max history entries to keep
MAX_HISTORY = 100
HISTORY_BLOB_NAME = "snipe-history.json"
HISTORY_CONTAINER = "sniper-data"


def _get_blob_client():
    """Get a blob client for the history file using the function's storage account."""
    try:
        conn_str = os.environ.get("AzureWebJobsStorage", "")
        if not conn_str:
            return None
        from azure.storage.blob import BlobServiceClient
        svc = BlobServiceClient.from_connection_string(conn_str)
        container = svc.get_container_client(HISTORY_CONTAINER)
        try:
            container.get_container_properties()
        except Exception:
            container.create_container()
        return container.get_blob_client(HISTORY_BLOB_NAME)
    except Exception as e:
        logging.warning("Failed to get blob client: %s", str(e))
        return None


def load_history():
    """Load history from blob storage. Returns list of dicts."""
    client = _get_blob_client()
    if not client:
        return []
    try:
        data = client.download_blob().readall()
        return json.loads(data)
    except Exception:
        return []


def save_history(history):
    """Save history to blob storage."""
    client = _get_blob_client()
    if not client:
        return
    try:
        # Trim to max
        trimmed = history[-MAX_HISTORY:]
        client.upload_blob(json.dumps(trimmed, default=str), overwrite=True)
    except Exception as e:
        logging.warning("Failed to save history: %s", str(e))


def log_cycle(result, regions_tried=None):
    """
    Log a snipe cycle result to history.
    result: dict from run_accumulator()
    regions_tried: list of region names attempted
    """
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "status": result.get("status", "unknown"),
        "total_landed": result.get("total_landed", 0),
        "target": result.get("target", 0),
        "remaining": result.get("remaining", 0),
        "actions_count": len(result.get("actions", [])),
        "actions": result.get("actions", [])[:5],  # Keep only first 5 actions per entry
        "regions_tried": regions_tried or [],
    }

    history = load_history()
    history.append(entry)
    save_history(history)
    return entry


def get_recent_history(count=20):
    """Get the most recent N history entries, newest first."""
    history = load_history()
    return list(reversed(history[-count:]))
