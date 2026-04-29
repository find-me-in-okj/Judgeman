"""
jm-gui.py — Judgeman GUI launcher (Windows, macOS, Linux)
Replaces the bash jm-gui script with a cross-platform Python equivalent.
"""
import sys, os, subprocess, webbrowser, time, threading, argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GUI_APP    = os.path.join(SCRIPT_DIR, 'gui', 'app.py')

sys.path.insert(0, os.path.join(SCRIPT_DIR, 'judgeman'))

BANNER = r"""
     _ _   _  ___  ___  ___ __  __ _   _  _
  _ | | | | ||   \/ __|| __||  \/  | /_\ | \| |
 | || | |_| || |) | (_ || _| | |\/| |/ _ \| .` |
  \__/ \___/ |___/ \___||___||_|  |_/_/ \_\_|\_|

  OSINT Analytical Reasoning Engine
"""

def main():
    parser = argparse.ArgumentParser(description='Judgeman GUI')
    parser.add_argument('--port', type=int, default=7432, help='Port (default: 7432)')
    parser.add_argument('--no-browser', action='store_true', help='Skip opening browser')
    args = parser.parse_args()

    port = args.port
    url  = f'http://127.0.0.1:{port}'

    print(BANNER)
    print(f'  Running at {url}')
    print('  Press Ctrl+C to quit\n')

    if not args.no_browser:
        def open_browser():
            time.sleep(1.4)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    env = os.environ.copy()
    env['JUDGEMAN_PORT'] = str(port)
    env['JUDGEMAN_HOME'] = os.path.join(os.path.expanduser('~'), '.judgeman')

    try:
        subprocess.run([sys.executable, GUI_APP], env=env)
    except KeyboardInterrupt:
        print('\n  Judgeman closed.\n')

if __name__ == '__main__':
    main()
