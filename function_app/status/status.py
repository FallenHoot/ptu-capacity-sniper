"""
Combined status + dashboard endpoint.
/api/status         -> HTML dashboard (browser) or JSON (API)
/api/status?run=true -> run a snipe cycle + show results
/api/status?json=true -> force JSON output
"""
import json
import os
import sys
import datetime
import traceback


def main(req):
    import azure.functions as func

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ptu_accumulator"))

    force_json = req.params.get("json", "").lower() == "true"
    run_manual = req.params.get("run", "").lower() == "true"
    fkey = req.params.get("code", "")
    accept = req.headers.get("Accept", "")
    want_html = ("text/html" in accept) and not force_json

    # Load config from env
    cfg = {
        "subscription": os.environ.get("AZURE_SUBSCRIPTION_ID", "")[:12] + "...",
        "resource_group": os.environ.get("AZURE_RESOURCE_GROUP", "NOT SET"),
        "account": os.environ.get("AZURE_ACCOUNT_NAME", "NOT SET"),
        "model": os.environ.get("PTU_MODEL_NAME", "gpt-5.2") + " (" + os.environ.get("PTU_MODEL_VERSION", "2025-12-11") + ")",
        "ptu_sku": os.environ.get("PTU_SKU_NAME", "DataZoneProvisionedManaged"),
        "target_ptus": os.environ.get("PTU_TARGET", "74"),
        "increment": "5",
        "min_ptu": "15",
        "max_deployments": os.environ.get("PTU_MAX_DEPLOYMENTS", "4"),
        "tpm_enabled": os.environ.get("TPM_ENABLED", "true"),
        "tpm_sku": os.environ.get("TPM_SKU_NAME", "Standard"),
        "data_zone": os.environ.get("DATA_ZONE", "eu"),
    }

    run_result = None
    if run_manual:
        try:
            import ptu_accumulator as acc
            run_result = acc.run_multi_region()
        except Exception as e:
            run_result = {"error": str(e)}

    # JSON response
    if not want_html:
        status = {"function": "PTU Capacity Sniper", "state": "running", "config": cfg}
        if run_result:
            status["manual_run"] = run_result
        else:
            status["hint"] = "Add ?run=true to trigger a manual snipe cycle"
        return func.HttpResponse(json.dumps(status, indent=2, default=str), mimetype="application/json", status_code=200)

    # HTML dashboard
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Config table
    config_rows = ""
    labels = {"subscription": "Subscription", "resource_group": "Resource Group", "account": "Foundry Account",
              "model": "Model", "ptu_sku": "PTU SKU", "target_ptus": "Target PTUs",
              "max_deployments": "Max Deployments", "tpm_enabled": "TPM Fallback", "tpm_sku": "TPM SKU",
              "data_zone": "Data Zone"}
    for key, label in labels.items():
        config_rows += '<tr><td class="cl">' + label + '</td><td><code>' + str(cfg.get(key, "")) + '</code></td></tr>'

    # Run result
    run_html = ""
    if run_result:
        if "error" in run_result:
            run_html = '<div class="a e">Error: ' + str(run_result.get("error", "")) + '</div>'
        else:
            total = run_result.get("total_landed", 0)
            target = run_result.get("target", 0)
            remaining = run_result.get("remaining", 0)
            actions = run_result.get("actions", [])
            regions = run_result.get("regions_tried", [])

            run_html += '<div class="m">'
            run_html += '<span class="mv">' + str(total) + '<br><span class="ml">Landed</span></span>'
            run_html += '<span class="mv">' + str(target) + '<br><span class="ml">Target</span></span>'
            run_html += '<span class="mv">' + str(remaining) + '<br><span class="ml">Remaining</span></span>'
            run_html += '<span class="mv">' + str(len(regions)) + '<br><span class="ml">Regions</span></span>'
            run_html += '</div>'

            if actions:
                run_html += '<div class="a s">Capacity sniped this cycle!</div>'
                run_html += '<table class="rt"><tr><th>Deployment</th><th>Action</th><th>Change</th><th>Gained</th></tr>'
                for a in actions:
                    region = a.get("region", "")
                    rb = '<span class="br">' + region + '</span> ' if region else ""
                    run_html += '<tr><td>' + rb + a.get("deployment", "?") + '</td>'
                    run_html += '<td><span class="b">' + a.get("action", "?") + '</span></td>'
                    run_html += '<td>' + str(a.get("previous", 0)) + ' &rarr; ' + str(a.get("new", 0)) + '</td>'
                    run_html += '<td class="g">+' + str(a.get("gained", 0)) + '</td></tr>'
                run_html += '</table>'
            else:
                run_html += '<div class="a w">No capacity available. Retrying in 5 min.</div>'
    else:
        run_html = '<div class="a w">Click Run Snipe Cycle to trigger a manual attempt.</div>'

    html = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>PTU Capacity Sniper</title><style>'
        '*{margin:0;padding:0;box-sizing:border-box}'
        'body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#0a0e17;color:#c9d1d9;line-height:1.6;min-height:100vh}'
        '.w{max-width:900px;margin:0 auto;padding:24px 20px}'
        '.hd{display:flex;justify-content:space-between;align-items:center;padding:16px 0;border-bottom:1px solid #21262d;margin-bottom:24px;flex-wrap:wrap;gap:12px}'
        '.hd h1{font-size:22px;font-weight:600;color:#f0f6fc}'
        '.hd h1 em{color:#58a6ff;font-style:normal}'
        '.pl{display:inline-flex;align-items:center;gap:6px;background:#0d1117;border:1px solid #30363d;border-radius:20px;padding:4px 12px;font-size:12px;color:#8b949e}'
        '.pl .d{width:7px;height:7px;border-radius:50%;background:#3fb950;animation:p 2s infinite}'
        '@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}'
        '.c{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:20px;margin-bottom:16px}'
        '.ch{font-size:13px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}'
        'table{width:100%;border-collapse:collapse}td{padding:6px 0;font-size:14px;border-bottom:1px solid #161b22}'
        '.cl{color:#8b949e;width:150px}'
        'code{background:#161b22;color:#58a6ff;padding:2px 8px;border-radius:4px;font-size:13px}'
        '.ac{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap}'
        '.bt{display:inline-flex;padding:8px 16px;border-radius:6px;font-size:13px;font-weight:500;text-decoration:none;border:1px solid #30363d;transition:all .15s}'
        '.bp{background:#238636;border-color:#238636;color:#fff}.bp:hover{background:#2ea043}'
        '.bd{background:#21262d;color:#c9d1d9}.bd:hover{background:#30363d}'
        '.a{padding:12px 16px;border-radius:8px;margin:10px 0;font-weight:600;font-size:14px}'
        '.s{background:#0d2912;color:#3fb950;border:1px solid #238636}'
        '.e{background:#2d0a0a;color:#f85149;border:1px solid #da3633}'
        '.w{background:#0c1929;color:#58a6ff;border:1px solid #1f6feb}'
        '.m{display:flex;gap:32px;margin:16px 0;flex-wrap:wrap}'
        '.mv{font-size:28px;font-weight:700;color:#f0f6fc;font-variant-numeric:tabular-nums}'
        '.ml{font-size:11px;color:#8b949e;text-transform:uppercase;font-weight:400}'
        '.rt{margin:12px 0}.rt th{text-align:left;padding:6px 8px;font-size:11px;color:#8b949e;text-transform:uppercase;border-bottom:1px solid #21262d}'
        '.rt td{padding:8px;font-size:13px;border-bottom:1px solid #161b22}'
        '.b{background:#161b22;color:#58a6ff;padding:2px 8px;border-radius:4px;font-size:12px}'
        '.br{background:#1c2333;color:#a371f7;padding:2px 8px;border-radius:4px;font-size:12px}'
        '.g{color:#3fb950;font-weight:700}'
        '.st{list-style:none;padding:0}.st li{padding:8px 0 8px 24px;position:relative;font-size:14px}'
        '.st li:before{content:"";position:absolute;left:0;top:14px;width:8px;height:8px;border-radius:50%;background:#30363d}'
        '.st li strong{color:#f0f6fc}'
        '.ft{margin-top:32px;padding-top:16px;border-top:1px solid #21262d;font-size:12px;color:#484f58;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}'
        '.ft a{color:#58a6ff;text-decoration:none}'
        '</style></head><body><div class="w">'

        '<div class="hd"><h1><em>&#9889;</em> PTU Capacity Sniper</h1>'
        '<span class="pl"><span class="d"></span>v2.0 | ' + now + '</span></div>'

        '<div class="ac">'
        '<a class="bt bp" href="/api/status?run=true' + ('&code=' + fkey if fkey else '') + '">&#9889; Run Snipe Cycle</a>'
        '<a class="bt bd" href="/api/status' + ('?code=' + fkey if fkey else '') + '">&#8635; Refresh</a>'
        '<a class="bt bd" href="/api/status?json=true' + ('&code=' + fkey if fkey else '') + '">{ } JSON</a>'
        '<a class="bt bd" href="/api/status?json=true&run=true' + ('&code=' + fkey if fkey else '') + '">{ } JSON + Run</a>'
        '</div>'

        + run_html +

        '<div class="c"><div class="ch">Configuration</div>'
        '<table>' + config_rows + '</table></div>'

        '<div class="c"><div class="ch">How it works</div>'
        '<ul class="st">'
        '<li><strong>Multi-Region</strong> &mdash; Cycles through all configured regions each run</li>'
        '<li><strong>PTU Snipe</strong> &mdash; +5 PTU on existing deployments, creates 15 PTU in empty slots</li>'
        '<li><strong>TPM Fallback</strong> &mdash; Regional Standard if no PTU capacity available</li>'
        '<li><strong>Auto-stop</strong> &mdash; Halts and alerts via Teams when target is reached</li>'
        '<li><strong>Schedule</strong> &mdash; Timer trigger runs every 5 minutes automatically</li>'
        '</ul></div>'

        '<div class="ft">'
        '<span>PTU Capacity Sniper v2.0 &mdash; Azure AI Foundry</span>'
        '<span><a href="https://learn.microsoft.com/azure/foundry/openai/concepts/provisioned-throughput">PTU Docs</a></span>'
        '</div></div></body></html>'
    )

    return func.HttpResponse(html, mimetype="text/html", status_code=200)
