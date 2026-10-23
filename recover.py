import json
import re
import sys

out_code = ""
with open(r"C:\Users\estre\.gemini\antigravity-ide\brain\cdb64dd9-dbab-4f14-a711-ed6185774349\.system_generated\logs\transcript_full.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        try:
            data = json.loads(line)
            if data.get("type") == "VIEW_FILE" and "logica_anomalias.py" in data.get("content", ""):
                if "def aplicar_alfas_formulaicos" in data["content"]:
                    content = data["content"]
                    # Extract the python code
                    # Removing the line numbers
                    lines = content.split("\n")
                    py_lines = []
                    for l in lines:
                        if re.match(r"^\d+:\s", l):
                            py_lines.append(l.split(": ", 1)[1])
                    out_code = "\n".join(py_lines)
        except Exception as e:
            pass

if out_code:
    with open(r"C:\Users\estre\.gemini\antigravity-ide\scratch\grvt_trading_bot\estrategias\recovered_logica.py", "w", encoding="utf-8") as f:
        f.write(out_code)
    print("Success")
else:
    print("Not found")
