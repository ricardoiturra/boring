"""Minimal Cloudflare v4 transport."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request


class Error(Exception):
    def __init__(self, message, code=3, http=None):
        super().__init__(message)
        self.code = code
        self.http = http


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Cloudflare:
    def __init__(self, token):
        if not re.fullmatch(r"[A-Za-z0-9_.~-]+", token):
            raise Error("Invalid API token file", 2)
        self.token = token
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, method, path, body=None, *, missing=False):
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4" + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=15) as response:
                envelope = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing:
                try:
                    codes = {item["code"] for item in json.load(exc)["errors"]}
                except (ValueError, KeyError, TypeError):
                    codes = set()
                # 81044 identifies a missing DNS record. Use the tunnel list
                # for tunnel existence because account and endpoint errors differ.
                if 81044 in codes:
                    return None
            raise Error(f"Cloudflare returned HTTP {exc.code} for {method} {path.split('?')[0]}",
                        http=exc.code) from None
        except (OSError, ValueError):
            raise Error(f"Cloudflare response unavailable for {method} {path.split('?')[0]}") from None
        if not isinstance(envelope, dict) or envelope.get("success") is not True or "result" not in envelope:
            raise Error(f"Cloudflare rejected {method} {path.split('?')[0]}")
        return envelope

    def get(self, path, *, missing=False):
        result = self.request("GET", path, missing=missing)
        return None if result is None else result["result"]

    def write(self, method, path, body=None):
        return self.request(method, path, body)["result"]

    def list(self, path, **filters):
        page = 1
        while True:
            query = urllib.parse.urlencode(dict(filters, page=page, per_page=100))
            envelope = self.request("GET", f"{path}?{query}")
            rows = envelope["result"]
            if not isinstance(rows, list):
                raise Error("Unexpected Cloudflare list response")
            yield from rows
            info = envelope.get("result_info") or {}
            pages = info.get("total_pages")
            page_size = info.get("per_page") or 100
            if not rows or (pages is not None and page >= pages) or (pages is None and len(rows) < page_size):
                return
            page += 1
