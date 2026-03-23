"""
Dashboard v2.0 -- Model-aware dynamic region filtering.
All HTML/CSS/JS inline. Zero external imports (same pattern as working /api/status).

When you change Model, SKU, or Zone the region checkboxes update instantly
via client-side JS driven by an embedded MODEL_DATA mapping sourced from
MS Learn (March 2026).
"""
import os
import sys
import json
import datetime
import traceback

# ---------------------------------------------------------------------------
# Model -> SKU -> Zone -> [regions]  (source: MS Learn, March 2026)
# ---------------------------------------------------------------------------
# All Azure AI Foundry EU regions
_ALL_EU = [
    "francecentral", "germanywestcentral", "italynorth", "northeurope",
    "norwayeast", "polandcentral", "spaincentral", "swedencentral",
    "switzerlandnorth", "switzerlandwest", "uksouth", "westeurope",
]
# All Azure AI Foundry US regions
_ALL_US = [
    "centralus", "eastus", "eastus2", "northcentralus",
    "southcentralus", "westus", "westus3",
]
# Data-zone EU/US subsets (data stays in-zone)
_DZ_EU = [
    "francecentral", "germanywestcentral", "italynorth", "polandcentral",
    "spaincentral", "swedencentral", "westeurope",
]
_DZ_US = [
    "centralus", "eastus", "eastus2", "northcentralus",
    "southcentralus", "westus", "westus3",
]

MODEL_DATA = {
    "gpt-5.4": {
        "version": "2026-03-05",
        "skus": {
            "GlobalProvisionedManaged": {"eu": _ALL_EU, "us": _ALL_US},
        },
    },
    "gpt-5.2": {
        "version": "2025-12-11",
        "skus": {
            "DataZoneProvisionedManaged": {"eu": _DZ_EU, "us": _DZ_US},
            "GlobalProvisionedManaged":   {"eu": _ALL_EU, "us": _ALL_US},
            "Standard": {
                "eu": ["francecentral", "swedencentral"],
                "us": ["centralus", "eastus", "eastus2"],
            },
        },
    },
    "gpt-5.1": {
        "version": "2025-11-13",
        "skus": {
            "DataZoneProvisionedManaged": {"eu": _DZ_EU, "us": _DZ_US},
            "GlobalProvisionedManaged":   {"eu": _ALL_EU, "us": _ALL_US},
            "DataZoneStandard":           {"eu": _DZ_EU, "us": _DZ_US},
            "Standard": {
                "eu": ["francecentral", "germanywestcentral", "northeurope",
                       "norwayeast", "swedencentral", "switzerlandnorth",
                       "uksouth", "westeurope"],
                "us": _ALL_US,
            },
        },
    },
    "gpt-5": {
        "version": "2025-08-07",
        "skus": {
            "DataZoneProvisionedManaged": {"eu": _DZ_EU, "us": _DZ_US},
            "GlobalProvisionedManaged":   {"eu": _ALL_EU, "us": _ALL_US},
            "ProvisionedManaged": {
                "eu": ["francecentral", "northeurope", "swedencentral",
                       "switzerlandnorth", "uksouth", "westeurope"],
                "us": _ALL_US,
            },
            "DataZoneStandard": {"eu": _DZ_EU, "us": _DZ_US},
            "Standard":         {"eu": _ALL_EU, "us": _ALL_US},
        },
    },
    "gpt-4.1": {
        "version": "2025-04-14",
        "skus": {
            "DataZoneProvisionedManaged": {"eu": _DZ_EU, "us": _DZ_US},
            "GlobalProvisionedManaged":   {"eu": _ALL_EU, "us": _ALL_US},
            "ProvisionedManaged": {
                "eu": ["francecentral", "northeurope", "swedencentral",
                       "switzerlandnorth", "uksouth", "westeurope"],
                "us": _ALL_US,
            },
            "DataZoneStandard": {"eu": _DZ_EU, "us": _DZ_US},
            "Standard":         {"eu": _ALL_EU, "us": _ALL_US},
        },
    },
}


# ---- Azure Function entry point -------------------------------------------

def main(req):
    import azure.functions as func

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ptu_accumulator"))

    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    fkey = req.params.get("code", "")
    run_manual = req.params.get("run", "").lower() == "true"
    save_msg = ""
    run_result = None

    # Handle POST (config save)
    if req.method == "POST":
        try:
            import urllib.parse
            body = req.get_body().decode("utf-8")
            form_data = {}
            for pair in body.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    form_data[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
            save_msg = _save_config(form_data)
        except Exception as e:
            save_msg = "Save failed: " + str(e)

    # Handle manual run
    if run_manual:
        try:
            import ptu_accumulator as acc
            run_result = acc.run_multi_region()
        except Exception as e:
            run_result = {"error": str(e)}

    # Read config from env
    cfg = {
        "AZURE_SUBSCRIPTION_ID": os.environ.get("AZURE_SUBSCRIPTION_ID", ""),
        "AZURE_RESOURCE_GROUP":  os.environ.get("AZURE_RESOURCE_GROUP", ""),
        "AZURE_ACCOUNT_NAME":    os.environ.get("AZURE_ACCOUNT_NAME", ""),
        "PTU_MODEL_NAME":        os.environ.get("PTU_MODEL_NAME", "gpt-5.2"),
        "PTU_MODEL_VERSION":     os.environ.get("PTU_MODEL_VERSION", "2025-12-11"),
        "PTU_SKU_NAME":          os.environ.get("PTU_SKU_NAME", "DataZoneProvisionedManaged"),
        "PTU_TARGET":            os.environ.get("PTU_TARGET", "74"),
        "PTU_MAX_DEPLOYMENTS":   os.environ.get("PTU_MAX_DEPLOYMENTS", "4"),
        "TPM_SKU_NAME":          os.environ.get("TPM_SKU_NAME", "Standard"),
        "TPM_CAPACITY":          os.environ.get("TPM_CAPACITY", "300"),
        "TPM_ENABLED":           os.environ.get("TPM_ENABLED", "true"),
        "TEAMS_WEBHOOK_URL":     os.environ.get("TEAMS_WEBHOOK_URL", ""),
        "DATA_ZONE":             os.environ.get("DATA_ZONE", "eu"),
        "SELECTED_REGIONS":      os.environ.get("SELECTED_REGIONS", ""),
    }

    # Always query live deployment status
    live_status = _get_live_deployments(cfg)

    try:
        html = _render(now, cfg, run_result, save_msg, live_status, fkey)
    except Exception:
        html = (
            "<html><body><h1>Render Error</h1><pre>"
            + traceback.format_exc()
            + "</pre></body></html>"
        )

    return func.HttpResponse(html, mimetype="text/html", status_code=200)


# ---- Config save via Azure Management API ----------------------------------

def _save_config(form_data):
    try:
        import requests
        from azure.identity import DefaultAzureCredential

        sub = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
        rg  = os.environ.get("AZURE_RESOURCE_GROUP", "")
        fn  = os.environ.get("AZURE_FUNCTION_APP_NAME", "")
        if not all([sub, rg, fn]):
            return "Error: Missing sub/rg/func name env vars"
        cred = DefaultAzureCredential()
        tok  = cred.get_token("https://management.azure.com/.default").token
        hdr  = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

        # Read current settings
        list_url = (
            "https://management.azure.com/subscriptions/" + sub
            + "/resourceGroups/" + rg
            + "/providers/Microsoft.Web/sites/" + fn
            + "/config/appsettings/list?api-version=2024-04-01"
        )
        r = requests.post(list_url, headers=hdr, timeout=30)
        if r.status_code != 200:
            return "Error reading settings: HTTP " + str(r.status_code)
        cur = r.json().get("properties", {})

        # Merge form values (skip region_ checkboxes, they go via SELECTED_REGIONS)
        for k, v in form_data.items():
            if not k.startswith("region_"):
                cur[k] = v

        # Write back
        put_url = (
            "https://management.azure.com/subscriptions/" + sub
            + "/resourceGroups/" + rg
            + "/providers/Microsoft.Web/sites/" + fn
            + "/config/appsettings?api-version=2024-04-01"
        )
        pr = requests.put(put_url, headers=hdr, json={"properties": cur}, timeout=30)
        if pr.status_code == 200:
            return "Configuration saved! Function will restart with new settings."
        return "Error saving: HTTP " + str(pr.status_code)
    except Exception as e:
        return "Save error: " + str(e)


# ---- Live deployment query from Azure API ----------------------------------

def _get_live_deployments(cfg):
    """Query Cognitive Services deployments API for current PTU/TPM state."""
    try:
        import requests
        from azure.identity import DefaultAzureCredential
        sub  = cfg.get("AZURE_SUBSCRIPTION_ID", "")
        rg   = cfg.get("AZURE_RESOURCE_GROUP", "")
        acct = cfg.get("AZURE_ACCOUNT_NAME", "")
        target = int(cfg.get("PTU_TARGET", "0") or "0")
        if not all([sub, rg, acct]):
            return None
        cred = DefaultAzureCredential()
        tok  = cred.get_token("https://management.azure.com/.default").token
        hdr  = {"Authorization": "Bearer " + tok}
        url  = (
            "https://management.azure.com/subscriptions/" + sub
            + "/resourceGroups/" + rg
            + "/providers/Microsoft.CognitiveServices/accounts/" + acct
            + "/deployments?api-version=2024-06-01-preview"
        )
        r = requests.get(url, headers=hdr, timeout=20)
        if r.status_code != 200:
            return None
        deps = []
        total_ptu = 0
        total_tpm = 0
        for d in r.json().get("value", []):
            props = d.get("properties", {})
            mobj  = props.get("model", {})
            sobj  = d.get("sku", {})
            sname = sobj.get("name", "")
            cap   = int(sobj.get("capacity", 0))
            is_ptu = "Provisioned" in sname
            if is_ptu:
                total_ptu += cap
            else:
                total_tpm += cap
            deps.append({
                "name": d.get("name", ""),
                "model": mobj.get("name", ""),
                "sku": sname,
                "capacity": cap,
                "state": props.get("provisioningState", ""),
                "region": props.get("rateLimits", [{}])[0].get("key", "") if props.get("rateLimits") else "",
                "is_ptu": is_ptu,
            })
        return {"deps": deps, "total_ptu": total_ptu, "total_tpm": total_tpm,
                "target": target, "remaining": max(0, target - total_ptu)}
    except Exception:
        return None


# ---- HTML renderer ---------------------------------------------------------

def _render(now, cfg, run_result, save_msg, live_status=None, fkey=""):
    model = cfg.get("PTU_MODEL_NAME", "gpt-5.2")
    sku   = cfg.get("PTU_SKU_NAME", "DataZoneProvisionedManaged")
    zone  = cfg.get("DATA_ZONE", "eu")

    sel_raw = cfg.get("SELECTED_REGIONS", "")
    try:
        selected = json.loads(sel_raw) if sel_raw else []
    except Exception:
        selected = []

    # Resolve initial regions from MODEL_DATA for server-side render
    md = MODEL_DATA.get(model, {})
    sd = md.get("skus", {}).get(sku, {})
    initial_regions = []
    if zone in ("eu", "all"):
        initial_regions += sd.get("eu", [])
    if zone in ("us", "all"):
        initial_regions += sd.get("us", [])
    if not selected:
        selected = initial_regions  # default: check all valid regions

    # ---- Live deployment status card ----------------------------------------
    live_html = ""
    if live_status:
        tp = live_status["total_ptu"]
        tt = live_status["total_tpm"]
        tg = live_status["target"]
        rm = live_status["remaining"]
        deps = live_status["deps"]
        pct = min(100, int((tp / tg * 100) if tg > 0 else 0))
        pct_c = "#3fb950" if pct >= 100 else "#58a6ff" if pct >= 50 else "#d29922"

        live_html += (
            '<div class="c"><div class="ch">Deployment Status &mdash; Live</div>'
            '<div class="m">'
            '<span class="mv">' + str(tp)  + '<br><span class="ml">PTUs Landed</span></span>'
            '<span class="mv">' + str(tg)  + '<br><span class="ml">Target</span></span>'
            '<span class="mv">' + str(rm)  + '<br><span class="ml">Remaining</span></span>'
        )
        if tt > 0:
            live_html += '<span class="mv">' + str(tt) + 'K<br><span class="ml">TPM Fallback</span></span>'
        live_html += '</div>'

        # Progress bar
        live_html += (
            '<div style="background:#161b22;border-radius:6px;height:8px;margin:8px 0 16px 0;overflow:hidden">'
            '<div style="background:' + pct_c + ';height:100%;width:' + str(pct)
            + '%;border-radius:6px;transition:width .3s"></div></div>'
        )

        if deps:
            live_html += '<table class="rt"><tr><th>Deployment</th><th>Model</th><th>SKU</th><th>Capacity</th><th>State</th></tr>'
            for d in deps:
                rgn = d.get("region", "")
                rb = '<span class="br">' + rgn + '</span> ' if rgn else ""
                cap_lbl = str(d["capacity"]) + " PTU" if d["is_ptu"] else str(d["capacity"]) + "K TPM"
                st_cls = "g" if d["state"] == "Succeeded" else "cl"
                live_html += (
                    '<tr><td>' + rb + d["name"]
                    + '</td><td><code>' + d["model"]
                    + '</code></td><td><span class="b">' + d["sku"]
                    + '</span></td><td class="g">' + cap_lbl
                    + '</td><td class="' + st_cls + '">' + d["state"]
                    + '</td></tr>'
                )
            live_html += '</table>'
        else:
            live_html += '<div class="a inf">No deployments found yet. Run a snipe cycle to start.</div>'
        live_html += '</div>'
    else:
        live_html = (
            '<div class="c"><div class="ch">Deployment Status</div>'
            '<div class="a inf">Could not query deployments. Check config below.</div></div>'
        )

    # ---- Config form rows (non-model fields) -------------------------------
    config_fields = [
        ("AZURE_SUBSCRIPTION_ID", "Subscription ID",   "text"),
        ("AZURE_RESOURCE_GROUP",  "Resource Group",     "text"),
        ("AZURE_ACCOUNT_NAME",    "Foundry Account",    "text"),
        ("PTU_TARGET",            "Target PTUs",        "number"),
        ("PTU_MAX_DEPLOYMENTS",   "Max Deploy/Region",  "number"),
        ("TPM_SKU_NAME",          "TPM SKU",            "select:Standard,DataZoneStandard"),
        ("TPM_CAPACITY",          "TPM Capacity (K)",   "number:13:10000"),
        ("TPM_ENABLED",           "TPM Fallback",       "select:true,false"),
        ("TEAMS_WEBHOOK_URL",     "Teams Webhook",      "text"),
    ]
    crow = ""
    for key, label, ft in config_fields:
        v = str(cfg.get(key, ""))
        if ft.startswith("select:"):
            opts = ft[7:].split(",")
            oh = ""
            for o in opts:
                s = " selected" if o == v else ""
                oh += '<option value="' + o + '"' + s + '>' + o + '</option>'
            inp = '<select name="' + key + '" class="input">' + oh + '</select>'
        elif ft.startswith("number"):
            nparts = ft.split(":")
            nmin = nparts[1] if len(nparts) > 1 else "1"
            nmax_attr = ' max="' + nparts[2] + '"' if len(nparts) > 2 else ""
            inp = '<input type="number" name="' + key + '" value="' + v + '" class="input" min="' + nmin + '"' + nmax_attr + '>'
        else:
            inp = '<input type="text" name="' + key + '" value="' + v + '" class="input">'
        crow += '<tr><td class="cl">' + label + '</td><td>' + inp + '</td></tr>\n'

    # ---- Model selector (with id for JS) -----------------------------------
    model_opts = ""
    for m in ["gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1"]:
        s = " selected" if m == model else ""
        model_opts += '<option value="' + m + '"' + s + '>' + m + '</option>'
    model_row = (
        '<tr><td class="cl">Model</td><td>'
        '<select name="PTU_MODEL_NAME" id="modelSel" class="input">'
        + model_opts + '</select></td></tr>\n'
    )

    # ---- Version input (with id for JS auto-fill) --------------------------
    ver = cfg.get("PTU_MODEL_VERSION", "")
    version_row = (
        '<tr><td class="cl">Version</td><td>'
        '<input type="text" name="PTU_MODEL_VERSION" id="versionInput" '
        'value="' + ver + '" class="input"></td></tr>\n'
    )

    # ---- PTU SKU selector (with id for JS) ---------------------------------
    all_skus = list(md.get("skus", {}).keys()) or ["DataZoneProvisionedManaged"]
    sku_opts = ""
    for sk in all_skus:
        s = " selected" if sk == sku else ""
        sku_opts += '<option value="' + sk + '"' + s + '>' + sk + '</option>'
    sku_row = (
        '<tr><td class="cl">PTU SKU</td><td>'
        '<select name="PTU_SKU_NAME" id="skuSel" class="input">'
        + sku_opts + '</select></td></tr>\n'
    )

    # ---- Zone selector (with id for JS) ------------------------------------
    zone_html = '<select name="DATA_ZONE" id="zoneSel" class="input" style="max-width:120px">'
    for z, lb in [("eu", "EU"), ("us", "US"), ("all", "All")]:
        s = " selected" if z == zone else ""
        zone_html += '<option value="' + z + '"' + s + '>' + lb + '</option>'
    zone_html += '</select>'

    # ---- Region checkboxes (server-rendered initial state) -----------------
    eu_set = set(sd.get("eu", []))
    rh = ""
    for r in initial_regions:
        ck = " checked" if r in selected else ""
        zt = "EU" if r in eu_set else "US"
        rh += (
            '<div class="ri"><input type="checkbox" name="region_' + r
            + '" value="' + r + '"' + ck + ' id="r_' + r
            + '"><label for="r_' + r + '">' + r
            + '</label><span class="zt">' + zt + '</span></div>'
        )
    if not initial_regions:
        rh = '<div style="color:#f85149;padding:12px">No regions available for this Model + SKU. Change Model or SKU above.</div>'

    # ---- Run result --------------------------------------------------------
    rr = ""
    if run_result:
        if "error" in run_result:
            rr = '<div class="a e">Error: ' + str(run_result.get("error", ""))[:500] + '</div>'
        else:
            t    = run_result.get("total_landed", 0)
            tg   = run_result.get("target", 0)
            rm   = run_result.get("remaining", 0)
            acts = run_result.get("actions", [])
            regs = run_result.get("regions_tried", [])
            rr += (
                '<div class="m">'
                '<span class="mv">' + str(t)  + '<br><span class="ml">Landed</span></span>'
                '<span class="mv">' + str(tg) + '<br><span class="ml">Target</span></span>'
                '<span class="mv">' + str(rm) + '<br><span class="ml">Remaining</span></span>'
                '<span class="mv">' + str(len(regs)) + '<br><span class="ml">Regions</span></span>'
                '</div>'
            )
            if acts:
                rr += '<div class="a s">Capacity sniped!</div><table class="rt"><tr><th>Deployment</th><th>Action</th><th>Change</th><th>Gained</th></tr>'
                for a in acts:
                    rgn = a.get("region", "")
                    rb = '<span class="br">' + rgn + '</span> ' if rgn else ""
                    rr += (
                        '<tr><td>' + rb + a.get("deployment", "?")
                        + '</td><td><span class="b">' + a.get("action", "?")
                        + '</span></td><td>' + str(a.get("previous", 0))
                        + ' &rarr; ' + str(a.get("new", 0))
                        + '</td><td class="g">+' + str(a.get("gained", 0))
                        + '</td></tr>'
                    )
                rr += '</table>'
            else:
                rr += '<div class="a inf">No capacity available across ' + str(len(regs)) + ' region(s). Retrying in 5 min.</div>'
    else:
        rr = '<div class="a inf">Click <strong>Run Snipe Cycle</strong> to trigger a manual attempt.</div>'

    # ---- Save message ------------------------------------------------------
    sh = ""
    if save_msg:
        cl = "s" if "saved" in save_msg.lower() or "success" in save_msg.lower() else "e"
        sh = '<div class="a ' + cl + '">' + save_msg + '</div>'

    # ---- Region count badge ------------------------------------------------
    region_count = str(len(initial_regions))

    # ---- JavaScript --------------------------------------------------------
    md_json = json.dumps(MODEL_DATA)
    js = (
        '<script>'
        'var MD=' + md_json + ';'
        'var modelSel=document.getElementById("modelSel");'
        'var skuSel=document.getElementById("skuSel");'
        'var zoneSel=document.getElementById("zoneSel");'
        'var versionInput=document.getElementById("versionInput");'
        'var regionBox=document.getElementById("regionBox");'
        'var regionCount=document.getElementById("regionCount");'

        'function updateSkus(){'
        '  var m=modelSel.value;'
        '  var md=MD[m]||{};'
        '  var skus=Object.keys(md.skus||{});'
        '  var prev=skuSel.value;'
        '  skuSel.innerHTML="";'
        '  skus.forEach(function(s){'
        '    var o=document.createElement("option");'
        '    o.value=s;o.textContent=s;'
        '    if(s===prev)o.selected=true;'
        '    skuSel.appendChild(o);'
        '  });'
        '  if(md.version)versionInput.value=md.version;'
        '  updateRegions();'
        '}'

        'function updateRegions(){'
        '  var m=modelSel.value;'
        '  var s=skuSel.value;'
        '  var z=zoneSel.value;'
        '  var md=MD[m]||{};'
        '  var sd=(md.skus||{})[s]||{};'
        '  var regions=[];'
        '  if(z==="eu"||z==="all")regions=regions.concat(sd.eu||[]);'
        '  if(z==="us"||z==="all")regions=regions.concat(sd.us||[]);'
        '  var euSet=new Set(sd.eu||[]);'
        '  var html="";'
        '  regions.forEach(function(r){'
        '    var zt=euSet.has(r)?"EU":"US";'
        '    html+=\'<div class="ri"><input type="checkbox" name="region_\'+r+\'" value="\'+r+\'" checked id="r_\'+r+\'"><label for="r_\'+r+\'">\'+r+\'</label><span class="zt">\'+zt+\'</span></div>\';'
        '  });'
        '  if(regions.length===0)html=\'<div style="color:#f85149;padding:12px">No regions for \'+m+\' + \'+s+\'. Try a different Model or SKU.</div>\';'
        '  regionBox.innerHTML=html;'
        '  regionCount.textContent=regions.length+" region"+(regions.length!==1?"s":"");'
        '}'

        'modelSel.addEventListener("change",updateSkus);'
        'skuSel.addEventListener("change",updateRegions);'
        'zoneSel.addEventListener("change",updateRegions);'

        'document.querySelector("form").addEventListener("submit",function(){'
        '  var c=[];'
        '  document.querySelectorAll("#regionBox input[type=checkbox]:checked").forEach(function(cb){c.push(cb.value)});'
        '  document.querySelector("input[name=SELECTED_REGIONS]").value=JSON.stringify(c);'
        '  document.querySelector("input[name=DATA_ZONE]").value=zoneSel.value;'
        '});'
        '</script>'
    )

    # ---- Assemble HTML -----------------------------------------------------
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>PTU Capacity Sniper</title>'
        '<style>'
        '*{margin:0;padding:0;box-sizing:border-box}'
        'body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#0a0e17;color:#c9d1d9;line-height:1.6;min-height:100vh}'
        '.wp{max-width:900px;margin:0 auto;padding:24px 20px}'
        '.hd{display:flex;justify-content:space-between;align-items:center;padding:16px 0;border-bottom:1px solid #21262d;margin-bottom:24px;flex-wrap:wrap;gap:12px}'
        '.hd h1{font-size:22px;font-weight:600;color:#f0f6fc}.hd h1 em{color:#58a6ff;font-style:normal}'
        '.pl{display:inline-flex;align-items:center;gap:6px;background:#0d1117;border:1px solid #30363d;border-radius:20px;padding:4px 12px;font-size:12px;color:#8b949e}'
        '.pl .d{width:7px;height:7px;border-radius:50%;background:#3fb950;animation:p 2s infinite}@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}'
        '.c{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:20px;margin-bottom:16px}'
        '.ch{font-size:13px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}'
        'table{width:100%;border-collapse:collapse}td,th{padding:6px 0;font-size:14px;border-bottom:1px solid #161b22}'
        '.cl{color:#8b949e;width:150px;vertical-align:middle}'
        '.input{background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 10px;font-size:13px;font-family:inherit;width:100%;max-width:320px}'
        '.input:focus{border-color:#58a6ff;outline:none;box-shadow:0 0 0 3px rgba(88,166,255,.15)}select.input{appearance:auto}'
        '.ac{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap}'
        '.bt{display:inline-flex;padding:8px 16px;border-radius:6px;font-size:13px;font-weight:500;text-decoration:none;border:1px solid #30363d;transition:all .15s;cursor:pointer;font-family:inherit;color:#c9d1d9}'
        '.bp{background:#238636;border-color:#238636;color:#fff}.bp:hover{background:#2ea043}'
        '.bb{background:#1f6feb;border-color:#1f6feb;color:#fff}.bb:hover{background:#388bfd}'
        '.bd{background:#21262d}.bd:hover{background:#30363d}'
        '.a{padding:12px 16px;border-radius:8px;margin:10px 0;font-weight:600;font-size:14px}'
        '.s{background:#0d2912;color:#3fb950;border:1px solid #238636}'
        '.e{background:#2d0a0a;color:#f85149;border:1px solid #da3633}'
        '.inf{background:#0c1929;color:#58a6ff;border:1px solid #1f6feb}'
        '.m{display:flex;gap:32px;margin:16px 0;flex-wrap:wrap}'
        '.mv{font-size:28px;font-weight:700;color:#f0f6fc;font-variant-numeric:tabular-nums}.ml{font-size:11px;color:#8b949e;text-transform:uppercase;font-weight:400}'
        '.rt{margin:12px 0}.rt th{text-align:left;padding:6px 8px;font-size:11px;color:#8b949e;text-transform:uppercase;border-bottom:1px solid #21262d}.rt td{padding:8px;font-size:13px;border-bottom:1px solid #161b22}'
        '.b{background:#161b22;color:#58a6ff;padding:2px 8px;border-radius:4px;font-size:12px}'
        '.br{background:#1c2333;color:#a371f7;padding:2px 8px;border-radius:4px;font-size:12px}'
        '.g{color:#3fb950;font-weight:700}'
        '.rg{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin:12px 0}'
        '.ri{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#161b22;border:1px solid #21262d;border-radius:6px;cursor:pointer}'
        '.ri:hover{border-color:#30363d;background:#1c2333}'
        '.ri input{accent-color:#58a6ff;width:16px;height:16px}'
        '.ri label{font-size:13px;cursor:pointer;flex:1}'
        '.zt{font-size:10px;color:#8b949e;background:#0d1117;padding:1px 6px;border-radius:3px}'
        '.badge{font-size:11px;color:#8b949e;background:#161b22;border:1px solid #21262d;border-radius:10px;padding:2px 8px;margin-left:6px}'
        '.st{list-style:none;padding:0}.st li{padding:8px 0 8px 24px;position:relative;font-size:14px}'
        '.st li:before{content:"";position:absolute;left:0;top:14px;width:8px;height:8px;border-radius:50%;background:#30363d}'
        '.st li strong{color:#f0f6fc}'
        '.ft{margin-top:32px;padding-top:16px;border-top:1px solid #21262d;font-size:12px;color:#484f58;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}'
        '.ft a{color:#58a6ff;text-decoration:none}'
        '.sub{color:#484f58;font-size:13px;margin-top:8px}'
        '</style></head><body><div class="wp">'

        # Header
        '<div class="hd"><h1><em>&#9889;</em> PTU Capacity Sniper</h1>'
        '<span class="pl"><span class="d"></span>v2.0 | ' + now + '</span></div>'

        # Action buttons (pass function key through all links)
        '<div class="ac">'
        '<a class="bt bp" href="/api/dashboard?run=true' + ('&code=' + fkey if fkey else '') + '">&#9889; Run Snipe Cycle</a>'
        '<a class="bt bd" href="/api/dashboard' + ('?code=' + fkey if fkey else '') + '">&#8635; Refresh</a>'
        '<a class="bt bd" href="/api/status?json=true' + ('&code=' + fkey if fkey else '') + '">{ } JSON</a>'
        '<a class="bt bd" href="/api/status?json=true&run=true' + ('&code=' + fkey if fkey else '') + '">{ } JSON + Run</a>'
        '</div>'

        + (live_html if not run_result else "") + sh + rr +

        # --- Form wraps Model/SKU config + regions + other config ---
        '<form method="POST" action="/api/dashboard' + ('?code=' + fkey if fkey else '') + '">'
        '<input type="hidden" name="SELECTED_REGIONS" value="">'
        '<input type="hidden" name="DATA_ZONE" value="' + zone + '">'

        # Model & Deployment card
        '<div class="c"><div class="ch">Model &amp; Deployment</div><table>'
        + model_row + version_row + sku_row
        + '</table></div>'

        # Target Regions card
        '<div class="c">'
        '<div class="ch">'
        '<span>Target Regions <span id="regionCount" class="badge">'
        + region_count + ' region' + ('s' if len(initial_regions) != 1 else '')
        + '</span></span>'
        '<span>Zone: ' + zone_html + '</span>'
        '</div>'
        '<div id="regionBox" class="rg">' + rh + '</div>'
        '</div>'

        # Other config card
        '<div class="c"><div class="ch">Configuration</div><table>'
        + crow
        + '</table>'
        '<div style="margin-top:16px;display:flex;gap:8px">'
        '<button type="submit" class="bt bb">Save Configuration</button>'
        '<span class="sub" style="align-self:center">Saves to Azure App Settings &amp; restarts function.</span>'
        '</div></div>'
        '</form>'

        # How it works
        '<div class="c"><div class="ch">How it works</div><ul class="st">'
        '<li><strong>Model-Aware</strong> &mdash; Regions auto-filter by Model + SKU from MS Learn data</li>'
        '<li><strong>Multi-Region</strong> &mdash; Cycles through all selected regions each run</li>'
        '<li><strong>PTU Snipe</strong> &mdash; +5 PTU on existing deployments, 15 PTU on empty slots</li>'
        '<li><strong>TPM Fallback</strong> &mdash; Regional Standard if no PTU capacity</li>'
        '<li><strong>Auto-stop</strong> &mdash; Halts + Teams alert at target</li>'
        '<li><strong>Schedule</strong> &mdash; Timer runs every 5 minutes</li>'
        '</ul></div>'

        # Footer
        '<div class="ft"><span>PTU Capacity Sniper v2.0 &mdash; Region data: MS Learn March 2026</span>'
        '<span><a href="https://learn.microsoft.com/azure/foundry/openai/concepts/provisioned-throughput">PTU Docs</a></span></div>'

        + js + '</div></body></html>'
    )
