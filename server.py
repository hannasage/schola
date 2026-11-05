#!/usr/bin/env python3
"""Local curriculum tracker. Python standard library only."""
import argparse
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
from local_store import LocalStore, StoreError, finite_json, strict_keys, validate_origin
import check_routes
import lesson_routes
from lesson_store import LessonStore

ROOT = Path(__file__).resolve().parent
import re
import sys
import traceback

class CurriculumError(ValueError):
    def __init__(self, message, field):
        super().__init__(message)
        self.field = field


def validate_curriculum(c):
    if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", c.get("id", "")):
        raise CurriculumError("Use a curriculum ID with lowercase letters, numbers, and hyphens.", "curriculum.id")
    for k in ("title", "subtitle", "description"):
        if not isinstance(c.get(k), str) or not c[k].strip():
            raise CurriculumError(f"Curriculum needs {k}.", f"curriculum.{k}")
    if not isinstance(c.get("resources"), dict):
        raise CurriculumError("Curriculum needs resources.", "curriculum.resources")
    if not isinstance(c.get("modules"), list) or not 1 <= len(c["modules"]) <= 100:
        raise CurriculumError("Curriculum needs 1–100 modules.", "curriculum.modules")
    if type(c.get("weeks")) is not int or not 1 <= c["weeks"] <= 520:
        raise CurriculumError("Invalid curriculum duration.", "curriculum.weeks")
    if type(c.get("weeklyHours")) not in (int, float) or not 1 <= c["weeklyHours"] <= 40:
        raise CurriculumError("Invalid weekly study hours.", "curriculum.weeklyHours")
    if "nextPhase" in c and (not isinstance(c["nextPhase"], list) or any(not isinstance(x, str) for x in c["nextPhase"])):
        raise CurriculumError("Next phase must be a list of text items.", "curriculum.nextPhase")
    if "researchDate" in c and not isinstance(c["researchDate"], str):
        raise CurriculumError("Research date must be text.", "curriculum.researchDate")
    ids = set()
    for key, r in c["resources"].items():
        for field in ("title", "url", "use"):
            if not isinstance(r, dict) or not isinstance(r.get(field), str):
                raise CurriculumError(f"Resource {key} needs {field}.", f"curriculum.resources.{key}.{field}")
        try:
            url = urlsplit(r["url"])
            valid = url.scheme in ("http", "https") and bool(url.hostname) and not url.username and not url.password
        except ValueError:
            valid = False
        if not valid:
            raise CurriculumError(f"Resource {key} needs an HTTP(S) URL without sign-in details.", f"curriculum.resources.{key}.url")
    for index, m in enumerate(c["modules"]):
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", m.get("id", "")) or m["id"] in ids:
            raise CurriculumError(f"Module {index + 1} needs a unique ID using letters, numbers, underscores, or hyphens.", f"curriculum.modules[{index}].id")
        ids.add(m["id"])
        for k in ("title", "phase", "question", "outcome", "math", "evidence", "challenge", "reading", "dataset"):
            if not isinstance(m.get(k), str):
                raise CurriculumError(f"Module {index + 1} needs {k}.", f"curriculum.modules[{index}].{k}")
        for k in ("tasks", "weeks", "resources", "prerequisites"):
            if not isinstance(m.get(k), list) or any(not isinstance(x, str) for x in m[k]):
                raise CurriculumError(f"Module {index + 1} has invalid {k}.", f"curriculum.modules[{index}].{k}")
        if not 1 <= len(m["tasks"]) <= 30:
            raise CurriculumError(f"Module {index + 1} needs 1–30 tasks.", f"curriculum.modules[{index}].tasks")
        if any(x not in c["resources"] for x in m["resources"]):
            raise CurriculumError(f"Module {index + 1} refers to an unknown resource.", f"curriculum.modules[{index}].resources")
        for k in ("hours", "weekStart", "weekEnd"):
            if type(m.get(k)) not in (int, float) or not 0 < m[k] <= 10000:
                raise CurriculumError(f"Module {index + 1} has invalid {k}.", f"curriculum.modules[{index}].{k}")
        if m["weekStart"] > m["weekEnd"] or m["weekEnd"] > c["weeks"]:
            raise CurriculumError(f"Module {index + 1} weeks must fit the curriculum duration.", f"curriculum.modules[{index}].weekEnd")
    for index, m in enumerate(c["modules"]):
        if any(p not in ids for p in m["prerequisites"]):
            raise CurriculumError(f"Module {index + 1} refers to an unknown prerequisite.", f"curriculum.modules[{index}].prerequisites")
    return c

def initial(c):
    return {"version": 1, "revision": 0, "curriculumId": c["id"], "weeklyHours": c["weeklyHours"], "startDate": "", "modules": {}, "experiments": []}

def validate(s, c):
    module_map = {m["id"]: m for m in c["modules"]}
    if not isinstance(s, dict) or s.get("version") != 1:
        raise ValueError("This file is not a version 1 Schola backup.")
    if s.get("curriculumId") != c["id"]:
        raise ValueError("This backup belongs to a different curriculum. Switch curricula before restoring it.")
    if type(s.get("revision")) is not int or s["revision"] < 0:
        raise ValueError("Invalid revision.")
    if type(s.get("weeklyHours")) not in (int, float) or not 1 <= s["weeklyHours"] <= 40:
        raise ValueError("Weekly hours must be between 1 and 40.")
    if not isinstance(s.get("startDate"), str) or len(s["startDate"]) > 10:
        raise ValueError("Invalid start date.")
    if s["startDate"]:
        from datetime import date
        date.fromisoformat(s["startDate"])
    if not isinstance(s.get("modules"), dict) or not isinstance(s.get("experiments"), list):
        raise ValueError("Invalid progress data.")
    for key, m in s["modules"].items():
        if key not in module_map or not isinstance(m, dict):
            raise ValueError("Unknown module.")
        if m.get("status") not in ("not-started", "active", "complete"):
            raise ValueError("Invalid module status.")
        if not isinstance(m.get("tasks"), list) or len(m["tasks"]) != len(module_map[key]["tasks"]) or any(type(x) is not bool for x in m["tasks"]):
            raise ValueError("Invalid task progress.")
        for field in ("notes", "evidence"):
            if not isinstance(m.get(field), str) or len(m[field]) > 50000:
                raise ValueError("Invalid notes or evidence.")
        if type(m.get("hours")) not in (int, float) or not 0 <= m["hours"] <= 10000:
            raise ValueError("Invalid study hours.")
        if m["status"] == "complete" and (not all(m["tasks"]) or not m["evidence"].strip()):
            raise ValueError("Complete all tasks and record evidence before completing a module.")
    if len(s["experiments"]) > 2000:
        raise ValueError("Too many experiments.")
    for e in s["experiments"]:
        if not isinstance(e, dict) or any(not isinstance(e.get(k), str) or len(e[k]) > 20000 for k in ("id", "date", "module", "title", "hypothesis", "result")):
            raise ValueError("Invalid experiment.")
        if e["module"] not in module_map or not e["title"].strip():
            raise ValueError("Invalid experiment title or module.")
    return s

class Handler(BaseHTTPRequestHandler):
    # A client that declares more body than it sends must not hold this single-threaded server forever.
    timeout = 10

    def log_message(self, *args):
        pass

    def respond(self, code, body, kind="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' https:; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in hosts and (not origin or origin == f"http://{host}")

    def store(self):
        built_in = json.loads((ROOT / "curriculum.json").read_text())
        return LocalStore(self.server.data_dir, built_in, validate_curriculum, validate, initial)

    def query(self):
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    def scope(self):
        query = self.query()
        fields = ("hubId", "backendOrigin", "accountId")
        if not any(k in query for k in fields):
            return None
        if any(len(query.get(k, [])) != 1 for k in fields):
            raise StoreError("Provide one hub, backend origin, and account together.", field="scope")
        return validate_origin(dict(kind="cloud", **{k: query[k][0] for k in fields}))

    def plan(self):
        values = self.query().get("plan", ["soccer-ml"])
        if len(values) != 1:
            raise StoreError("Choose one curriculum.", field="plan")
        return values[0]

    def curricula(self):
        store = self.store()
        return [store.get_record(r["recordId"])["curriculum"] for r in store.library()["records"]]

    def current(self):
        return self.store().local_record(self.plan())["curriculum"]

    def read_state(self, c):
        return self.store().local_record(c["id"])["state"]

    def fail(self, error):
        if isinstance(error, StoreError):
            return self.respond(error.status, error.payload())
        if isinstance(error, OSError):
            return self.respond(500, {"error": "Cannot access saved data. Keep your edits and check that the data folder is writable.", "code": "storage_unavailable", "retryable": True})
        return self.respond(400, {"error": str(error), "code": "invalid_request", "retryable": False})

    def lesson_response(self, call):
        """Run a lesson route. A named problem keeps its status. Anything else is a server fault: 500, no detail."""
        try:
            return self.respond(*call())
        except StoreError as error:
            return self.fail(error)
        except OSError as error:
            return self.fail(error)
        except Exception:  # noqa: BLE001 - any other fault, including bad stored data, must answer in JSON
            traceback.print_exc(file=sys.stderr)
            return self.respond(500, {"error": "Schola hit an unexpected problem and could not finish. Check your lessons before you try again.", "code": "internal_error", "retryable": False})

    def forbidden(self):
        return self.respond(403, {"error": "Use the local application address.", "code": "forbidden_origin", "retryable": False})

    def not_found(self):
        return self.respond(404, {"error": "Not found.", "code": "not_found", "retryable": False})

    def lessons(self):
        return LessonStore(self.server.data_dir)

    def drain(self, size):
        """Read and drop the body of a request that is too big, so the client can still read the answer.

        A client that sends a body while the server answers and closes would see a reset instead of the
        413. Only a bounded amount is read, and only while data keeps arriving.
        """
        if size > 16 * 1024 * 1024:
            return
        self.connection.settimeout(0.25)
        remaining = size
        try:
            while remaining > 0:
                chunk = self.rfile.read1(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(self.timeout)

    def body(self, limit=2_000_000):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise StoreError("Expected JSON.", "unsupported_media_type", 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise StoreError("Invalid content length.", "invalid_request") from None
        if not 0 < size <= limit:
            if size > limit:
                self.drain(size)
            raise StoreError({2_000_000: "Backup must be smaller than 2 MB.", lesson_routes.BODY_LIMIT: "Lesson files must be smaller than 5 MB."}.get(limit, "This request must be smaller than 64 KB."), "payload_too_large", 413)
        try:
            raw = self.rfile.read(size)
        except TimeoutError:
            raise StoreError("The request body did not arrive in time.", "request_timeout", 408) from None
        try:
            result = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError):
            raise StoreError("Request must contain valid, finite JSON.", "invalid_json") from None
        finite_json(result)
        return result

    def do_GET(self):
        if not self.allowed():
            return self.forbidden()
        path = urlsplit(self.path).path
        try:
            if path in check_routes.GET_ROUTES:
                return self.lesson_response(lambda: check_routes.handle_get(self.server, self.lessons(), path, self.headers))
            if path in lesson_routes.GET_ROUTES:
                return self.lesson_response(lambda: lesson_routes.handle_get(self.lessons(), path))
            if path == "/api/recovery":
                return self.respond(200, self.store().recovery_options())
            if path == "/api/cloud-config":
                config_file = ROOT / "cloud-config.local.json"
                config = json.loads(config_file.read_text()) if config_file.exists() else {}
                return self.respond(200, {k: config.get(k, "") for k in ("url", "publishableKey")})
            if path == "/api/curricula":
                return self.respond(200, self.curricula())
            if path == "/api/state":
                return self.respond(200, self.store().local_record(self.plan())["state"])
            if path == "/api/record":
                return self.respond(200, self.store().local_record(self.plan()))
            if path == "/api/library":
                return self.respond(200, self.store().library(self.scope()))
            match = re.fullmatch(r"/api/records/([^/]+)(/export)?", path)
            if match:
                return self.respond(200, self.store().get_record(match[1], self.scope()))
            files = {"/": ("index.html", "text/html; charset=utf-8"), "/auth/return": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"), "/curriculum.json": ("curriculum.json", "application/json"), "/web.bundle.js": ("web.bundle.js", "text/javascript"), "/web.bundle.css": ("web.bundle.css", "text/css"), "/assets/appearance/syne-latin-variable.woff2": ("assets/appearance/syne-latin-variable.woff2", "font/woff2"), "/assets/appearance/ibm-plex-sans-latin-variable.woff2": ("assets/appearance/ibm-plex-sans-latin-variable.woff2", "font/woff2")}
            if path not in files:
                return self.not_found()
            name, kind = files[path]
            return self.respond(200, (ROOT / name).read_bytes(), kind)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            return self.fail(error)

    def do_PUT(self):
        if not self.allowed():
            return self.forbidden()
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/api/records/([^/]+)", path)
        if path not in ("/api/state", "/api/curricula", "/api/library") and not match:
            return self.not_found()
        try:
            payload = self.body()
            store = self.store()
            if path == "/api/curricula":
                c = store._course(payload)
                record = store.import_record({"curriculum": c, "state": initial(c)}, {"kind": "local"}, "active")
                return self.respond(201, record["curriculum"])
            if path == "/api/state":
                return self.respond(200, store.save_legacy_state(self.plan(), payload))
            if path == "/api/library":
                strict_keys(payload, ["expectedRevision", "focusRecordId"], "request")
                return self.respond(200, store.save_library(payload["expectedRevision"], payload["focusRecordId"], self.scope()))
            strict_keys(payload, ["expectedRevision", "record"], "request")
            return self.respond(200, store.save_record(match[1], payload["expectedRevision"], payload["record"], self.scope()))
        except (OSError, ValueError, TypeError, RecursionError) as error:
            return self.fail(error)

    def do_POST(self):
        if not self.allowed():
            return self.forbidden()
        route = urlsplit(self.path).path
        lesson = route in lesson_routes.POST_ROUTES
        check = route in check_routes.POST_ROUTES
        if not lesson and not check and route not in ("/api/import", "/api/import/preview", "/api/recovery"):
            return self.not_found()
        try:
            payload = self.body(lesson_routes.BODY_LIMIT if lesson else check_routes.BODY_LIMIT if check else 2_000_000)
            if check:
                return self.lesson_response(lambda: check_routes.handle_post(self.server, self.lessons(), route, payload, self.headers))
            if lesson:
                return self.lesson_response(lambda: lesson_routes.handle_post(self.lessons(), route, payload, self.store() if route.endswith("/migrate") else None))
            if urlsplit(self.path).path == "/api/recovery":
                strict_keys(payload, ["recordId", "damagedDigest"], "request")
                return self.respond(200, self.store().recover_record(payload["recordId"], payload["damagedDigest"]))
            required = {"source", "origin", "status"}
            if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"targetRecordId", "expectedRevision"}:
                raise StoreError("Invalid import request fields.", field="request")
            scope = self.scope()
            origin = validate_origin(payload["origin"])
            if origin["kind"] == "cloud" and origin != scope:
                raise StoreError("Use the matching connection scope for a cloud import.", "scope_mismatch", 400, field="scope")
            operation = self.store().preview_import if urlsplit(self.path).path == "/api/import/preview" else self.store().import_record
            record = operation(payload["source"], origin, payload["status"], payload.get("targetRecordId"), payload.get("expectedRevision"), scope)
            return self.respond(200 if urlsplit(self.path).path.endswith("/preview") or payload.get("targetRecordId") is not None else 201, record)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            return self.fail(error)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    server.data_dir = args.data_dir
    check_routes.session_for(server)  # the check token is made when the server starts
    print(f"Schola is ready at http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
