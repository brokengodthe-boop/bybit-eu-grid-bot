from flask import Flask, render_template_string, jsonify
import os
import glob
import json
from src.bybit_client import BybitClient

app = Flask(__name__)

# Config
LOGS_DIR = r"C:\Users\estre\.gemini\antigravity-ide\brain\cdb64dd9-dbab-4f14-a711-ed6185774349\.system_generated\tasks"
AUDIT_FILE = "logs/auditoria.json"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Antigravity Terminal</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=JetBrains+Mono:wght@400;700&display=swap');
        
        body {
            background-color: #050505;
            background-image: 
                radial-gradient(at 0% 0%, rgba(17, 24, 39, 1) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(16, 185, 129, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(59, 130, 246, 0.1) 0px, transparent 50%);
            color: #f8fafc;
            font-family: 'Outfit', sans-serif;
            min-height: 100vh;
        }
        
        .glass-card {
            background: rgba(20, 25, 35, 0.6);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.05);
            box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5);
            border-radius: 1.5rem;
        }

        .terminal-box {
            font-family: 'JetBrains Mono', monospace;
            background-color: #0a0a0a;
            color: #34d399;
            overflow-y: auto;
            height: 350px;
            white-space: pre-wrap;
            border: 1px solid rgba(16, 185, 129, 0.2);
            border-radius: 1rem;
            box-shadow: inset 0 0 20px rgba(0,0,0,0.8);
        }

        /* Scrollbar aesthetics */
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #0f172a; border-radius: 4px; }
        ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #475569; }

        .pulse-dot {
            height: 12px; width: 12px;
            background-color: #10b981;
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 12px #10b981;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { box-shadow: 0 0 0 10px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }
    </style>
</head>
<body class="p-4 md:p-8">
    <div class="max-w-7xl mx-auto space-y-8">
        
        <!-- HEADER -->
        <header class="flex flex-col md:flex-row justify-between items-center glass-card p-6 md:px-10">
            <div class="flex items-center space-x-6 mb-4 md:mb-0">
                <div class="h-16 w-16 bg-gradient-to-br from-emerald-400 to-blue-500 rounded-2xl flex items-center justify-center shadow-lg shadow-emerald-500/20">
                    <svg class="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
                </div>
                <div>
                    <h1 class="text-4xl font-extrabold tracking-tight text-white">Antigravity V3.1</h1>
                    <div class="flex items-center mt-2 space-x-2">
                        <span class="pulse-dot"></span>
                        <span class="text-emerald-400 font-semibold uppercase tracking-wider text-sm">Quant Core En Línea</span>
                    </div>
                </div>
            </div>
            <div class="text-center md:text-right">
                <p class="text-slate-400 font-semibold uppercase tracking-wider text-xs mb-1">Capital Operativo (USDC)</p>
                <h2 class="text-6xl font-black text-transparent bg-clip-text bg-gradient-to-r from-white to-slate-400" id="total_equity">...</h2>
            </div>
        </header>

        <!-- MIDDLE METRICS -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
            
            <!-- LIQUIDITY WALLET -->
            <div class="glass-card p-6 flex flex-col">
                <div class="flex justify-between items-center mb-6">
                    <h3 class="text-xl font-bold text-white">Caja Fuerte (Spot)</h3>
                    <svg class="w-6 h-6 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z"></path></svg>
                </div>
                <div id="wallet_container" class="space-y-4 flex-grow">
                    <!-- Dinámico -->
                </div>
            </div>

            <!-- ACTIVE TRADES -->
            <div class="glass-card p-6 lg:col-span-2 flex flex-col">
                <div class="flex justify-between items-center mb-6">
                    <h3 class="text-xl font-bold text-white">Francotirador (Posiciones)</h3>
                    <span class="bg-blue-500/10 text-blue-400 border border-blue-500/30 px-3 py-1 rounded-full text-xs font-bold uppercase">Buscando Objetivos</span>
                </div>
                <div id="trades_container" class="space-y-4">
                    <!-- Dinámico -->
                </div>
            </div>
        </div>

        <!-- AI TERMINAL -->
        <div class="glass-card p-6">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xl font-bold text-white flex items-center">
                    <svg class="w-5 h-5 mr-2 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>
                    Cerebro IA (Gemini 1.5)
                </h3>
            </div>
            <div id="terminal_logs" class="terminal-box p-5 text-sm">
                Inicializando red neuronal...
            </div>
        </div>
        
    </div>

    <script>
        function updateDashboard() {
            fetch('/api/data')
                .then(response => response.json())
                .then(data => {
                    // Update Equity
                    document.getElementById('total_equity').innerText = '$' + parseFloat(data.equity).toFixed(2);
                    
                    // Update Wallet
                    let walletHtml = '';
                    data.wallet.forEach(c => {
                        let isUSDC = c.coin === 'USDC';
                        let accent = isUSDC ? 'text-emerald-400' : 'text-slate-300';
                        let bg = isUSDC ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-slate-800/50 border-slate-700';
                        
                        walletHtml += `
                        <div class="flex justify-between items-center p-4 rounded-xl border ${bg}">
                            <div class="flex items-center space-x-3">
                                <div class="w-10 h-10 rounded-full flex items-center justify-center bg-slate-900 border border-slate-700 font-bold ${accent}">
                                    ${c.coin.substring(0,3)}
                                </div>
                                <span class="font-bold text-lg text-white">${c.coin}</span>
                            </div>
                            <div class="text-right">
                                <span class="block font-black text-xl text-white">${parseFloat(c.qty).toFixed(4)}</span>
                                <span class="text-xs text-slate-400 font-mono">$${parseFloat(c.usd_val).toFixed(2)} USD</span>
                            </div>
                        </div>`;
                    });
                    document.getElementById('wallet_container').innerHTML = walletHtml || '<p class="text-slate-500 text-center mt-10">Sin liquidez</p>';
                    
                    // Update Trades
                    let tradesHtml = '';
                    if (data.trades && data.trades.length > 0) {
                        data.trades.forEach(t => {
                            tradesHtml += `
                            <div class="p-5 rounded-2xl bg-gradient-to-r from-slate-800 to-slate-900 border border-slate-700 flex flex-col md:flex-row justify-between items-start md:items-center">
                                <div class="flex items-center space-x-4 mb-4 md:mb-0">
                                    <div class="w-12 h-12 rounded-xl bg-emerald-500/20 flex items-center justify-center">
                                        <svg class="w-6 h-6 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"></path></svg>
                                    </div>
                                    <div>
                                        <h4 class="font-black text-2xl text-white">${t.symbol}</h4>
                                        <span class="text-emerald-400 text-xs font-bold uppercase tracking-wider">LONG (COMPRA)</span>
                                    </div>
                                </div>
                                
                                <div class="flex space-x-6 text-center">
                                    <div>
                                        <p class="text-slate-500 text-xs uppercase font-bold mb-1">Entrada</p>
                                        <p class="text-white font-mono font-bold">$${t.entry_price}</p>
                                    </div>
                                    <div>
                                        <p class="text-emerald-500 text-xs uppercase font-bold mb-1">Target (TP)</p>
                                        <p class="text-emerald-400 font-mono font-bold">$${t.tp_price}</p>
                                    </div>
                                    <div>
                                        <p class="text-red-500 text-xs uppercase font-bold mb-1">Riesgo (SL)</p>
                                        <p class="text-red-400 font-mono font-bold">$${t.sl_price}</p>
                                    </div>
                                </div>
                            </div>`;
                        });
                    } else {
                        tradesHtml = `
                        <div class="flex flex-col items-center justify-center h-32 border border-dashed border-slate-600 rounded-2xl bg-slate-800/30">
                            <svg class="w-8 h-8 text-slate-500 mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                            <p class="text-slate-400 font-medium text-sm">No hay posiciones activas. Escaneando 96 criptoactivos...</p>
                        </div>`;
                    }
                    document.getElementById('trades_container').innerHTML = tradesHtml;
                    
                    // Update Logs
                    const logBox = document.getElementById('terminal_logs');
                    const wasScrolledToBottom = logBox.scrollHeight - logBox.clientHeight <= logBox.scrollTop + 1;
                    logBox.innerHTML = data.logs;
                    if (wasScrolledToBottom) {
                        logBox.scrollTop = logBox.scrollHeight;
                    }
                })
                .catch(error => console.error('Error fetching data:', error));
        }

        setInterval(updateDashboard, 2000);
        updateDashboard();
    </script>
</body>
</html>
"""

def get_latest_log_file():
    list_of_files = glob.glob(os.path.join(LOGS_DIR, '*.log'))
    if not list_of_files:
        return None
    latest_file = max(list_of_files, key=os.path.getctime)
    return latest_file

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/data')
def api_data():
    try:
        b = BybitClient(testnet=False)
        res = b.session.get_wallet_balance(accountType='UNIFIED')
        coins = res.get('result', {}).get('list', [{}])[0].get('coin', [])
        equity = res.get('result', {}).get('list', [{}])[0].get('totalEquity', '0')
        
        wallet = []
        for c in coins:
            usd_val = float(c.get('usdValue', 0))
            if usd_val > 0.1:
                wallet.append({'coin': c.get('coin'), 'qty': c.get('walletBalance'), 'usd_val': usd_val})
                
        # Read Logs
        log_content = "Esperando logs del bot..."
        latest_log = get_latest_log_file()
        if latest_log:
            with open(latest_log, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                log_content = "".join(lines[-40:]) # last 40 lines
                
        # Read Open Trades
        trades = []
        if os.path.exists(AUDIT_FILE):
            with open(AUDIT_FILE, 'r') as f:
                audit_list = json.load(f)
                for data in audit_list:
                    if not data.get('cerrado', False):
                        trades.append({
                            'symbol': data.get('symbol', 'UNKNOWN'),
                            'side': data.get('side', 'Buy'),
                            'entry_price': data.get('entry_price'),
                            'tp_price': data.get('take_profit'),
                            'sl_price': data.get('stop_loss')
                        })
        
        return jsonify({
            'equity': equity,
            'wallet': wallet,
            'logs': log_content,
            'trades': trades
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
