"""Создаёт (или обновляет) задание на cron-job.org, которое раз в 10 минут запускает бота
через GitHub API (workflow_dispatch). Ключи берёт из .env:
  CRONJOB_API_KEY       — ключ API cron-job.org (Settings → API)
  GITHUB_DISPATCH_TOKEN — fine-grained токен GitHub с правом Actions: write только на freelance-bot
Запуск: python tools/setup_cronjob.py [--status]
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
with open(os.path.join(ROOT, ".env"), encoding="utf-8-sig") as f:
    for line in f:
        k, sep, v = line.strip().partition("=")
        if sep:
            env[k] = v

API = "https://api.cron-job.org"
TITLE = "freelance-bot: проверка заказов"
TARGET = "https://api.github.com/repos/mrudman87-blip/freelance-bot/actions/workflows/check.yml/dispatches"


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + env["CRONJOB_API_KEY"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


job = {
    "url": TARGET,
    "enabled": True,
    "title": TITLE,
    "saveResponses": True,
    "requestMethod": 1,  # POST
    "requestTimeout": 30,
    "schedule": {"timezone": "Europe/Moscow", "expiresAt": 0, "hours": [-1], "mdays": [-1],
                 "months": [-1], "wdays": [-1], "minutes": [3, 13, 23, 33, 43, 53]},
    "notification": {"onFailure": True, "onSuccess": False, "onDisable": True},
    "extendedData": {
        "headers": {"Authorization": "Bearer " + env["GITHUB_DISPATCH_TOKEN"],
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "cron-job-freelance-bot"},
        "body": json.dumps({"ref": "main"}),
    },
}

jobs = call("GET", "/jobs").get("jobs", [])
mine = [j for j in jobs if j.get("title") == TITLE]

if "--status" in sys.argv:
    for j in mine:
        print("job", j["jobId"], "enabled", j["enabled"], "lastStatus", j.get("lastStatus"),
              "lastExecution", j.get("lastExecution"), "nextExecution", j.get("nextExecution"))
        hist = call("GET", f"/jobs/{j['jobId']}/history").get("history", [])
        for h in hist[:5]:
            print("   ", h.get("date"), "HTTP", h.get("httpStatus"), h.get("statusText"))
    sys.exit()

if mine:
    jid = mine[0]["jobId"]
    call("PATCH", f"/jobs/{jid}", {"job": job})
    print("обновлено задание", jid)
else:
    jid = call("PUT", "/jobs", {"job": job})["jobId"]
    print("создано задание", jid)
