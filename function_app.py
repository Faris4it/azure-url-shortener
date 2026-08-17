"""
Azure URL Shortener
--------------------
Two HTTP-triggered Azure Functions (Python v2 programming model):

  POST /api/shorten        -> create a short code for a URL
  GET  /api/{short_code}   -> redirect to the original URL

URLs are persisted in a single Azure Table Storage table, using one
fixed partition (PartitionKey="url") and the short code as the RowKey.
That's a reasonable tradeoff at portfolio/demo scale: it keeps lookups
to a single point read, at the cost of all writes landing in one
partition (fine for low-volume traffic, not for high-throughput prod).
"""

import json
import logging
import secrets
import string
from urllib.parse import urlparse

import azure.functions as func
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

TABLE_NAME = "shorturls"
PARTITION_KEY = "url"
SHORT_CODE_LENGTH = 7
SHORT_CODE_ALPHABET = string.ascii_letters + string.digits
MAX_GENERATION_ATTEMPTS = 5


def _get_table_client():
    """Build a TableClient from the Functions storage connection string.

    Reuses AzureWebJobsStorage (already required by the runtime) instead
    of introducing a second connection string, so local.settings.json
    only needs one storage value.
    """
    import os

    connection_string = os.environ["AzureWebJobsStorage"]
    service_client = TableServiceClient.from_connection_string(connection_string)
    return service_client.create_table_if_not_exists(TABLE_NAME)


def _is_valid_url(candidate: str) -> bool:
    """Accept only well-formed http/https URLs."""
    try:
        result = urlparse(candidate)
    except ValueError:
        return False
    return result.scheme in ("http", "https") and bool(result.netloc)


def _generate_short_code() -> str:
    return "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_LENGTH))


@app.route(route="shorten", methods=["POST"])
def shorten(req: func.HttpRequest) -> func.HttpResponse:
    """POST /api/shorten  {"url": "https://example.com"} -> {"short_code", "short_url"}"""
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Request body must be valid JSON."}),
            status_code=400,
            mimetype="application/json",
        )

    original_url = body.get("url") if isinstance(body, dict) else None
    if not original_url:
        return func.HttpResponse(
            json.dumps({"error": "Missing required field 'url'."}),
            status_code=400,
            mimetype="application/json",
        )

    if not _is_valid_url(original_url):
        return func.HttpResponse(
            json.dumps({"error": "'url' must be a valid http(s) URL."}),
            status_code=400,
            mimetype="application/json",
        )

    table_client = _get_table_client()

    # Retry on the (rare) chance of a short-code collision rather than
    # trusting randomness alone.
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        short_code = _generate_short_code()
        entity = {
            "PartitionKey": PARTITION_KEY,
            "RowKey": short_code,
            "OriginalUrl": original_url,
        }
        try:
            table_client.create_entity(entity=entity)
            break
        except ResourceExistsError:
            logging.warning("Short code collision on attempt %d, retrying.", attempt + 1)
            continue
    else:
        return func.HttpResponse(
            json.dumps({"error": "Could not generate a unique short code, try again."}),
            status_code=500,
            mimetype="application/json",
        )

    # Build the short URL from the incoming request's own scheme/host so
    # it works unchanged in local dev and after deployment.
    parsed_request = urlparse(req.url)
    base_url = f"{parsed_request.scheme}://{parsed_request.netloc}"
    short_url = f"{base_url}/api/{short_code}"

    return func.HttpResponse(
        json.dumps({"short_code": short_code, "short_url": short_url}),
        status_code=201,
        mimetype="application/json",
    )


@app.route(route="{short_code}", methods=["GET"])
def redirect_to_original(req: func.HttpRequest) -> func.HttpResponse:
    """GET /api/{short_code} -> 302 redirect to the original URL, or 404."""
    short_code = req.route_params.get("short_code")

    table_client = _get_table_client()
    try:
        entity = table_client.get_entity(partition_key=PARTITION_KEY, row_key=short_code)
    except ResourceNotFoundError:
        return func.HttpResponse(
            json.dumps({"error": f"No URL found for short code '{short_code}'."}),
            status_code=404,
            mimetype="application/json",
        )

    return func.HttpResponse(
        status_code=302,
        headers={"Location": entity["OriginalUrl"]},
    )
