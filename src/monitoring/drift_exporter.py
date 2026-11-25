"""Serve aggregate drift metrics and Evidently HTML, never input CSVs."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path("artifacts/stage6/drift")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = (
                "<h1>Stage 6: ONNX drift experiments (simulated)</h1>"
                + "".join(
                    f'<p><a href="/{name}/evidently.html">{name}</a></p>'
                    for name in ("baseline", "data-drift", "concept-drift")
                )
            ).encode()
            kind = "text/html; charset=utf-8"
        elif self.path == "/metrics":
            text = ""
            for scenario in ("baseline", "data-drift", "concept-drift"):
                path = ROOT / scenario / "metrics.prom"
                if path.is_file():
                    for line in path.read_text().splitlines():
                        name, value = line.split()
                        text += f'{name}{{scenario="{scenario}"}} {value}\n'
            body = text.encode()
            kind = "text/plain; version=0.0.4"
        elif self.path in [
            f"/{name}/evidently.html"
            for name in ("baseline", "data-drift", "concept-drift")
        ]:
            path = ROOT / self.path.lstrip("/")
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
            kind = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
