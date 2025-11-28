import os
import sys
import subprocess
import platform
import glob
import shutil
from pathlib import Path
from PyQt5.QtWidgets import QMessageBox, QApplication, QProgressDialog
from qgis.PyQt.QtCore import QTimer, Qt, QProcess, QCoreApplication
from qgis.core import QgsProject

def get_macos_version():
    """Get macOS version information"""
    try:
        result = subprocess.run(['sw_vers', '-productVersion'], 
                              capture_output=True, text=True)
        version = result.stdout.strip()
        major, minor = version.split('.')[:2]
        return float(f"{major}.{minor}")
    except:
        return None

def find_qgis_python_macos():
    """Find QGIS Python paths using various methods on macOS"""
    found_paths = {
        'python': None,
        'pip': None,
        'qgis_app': None
    }
    
    # 1. Currently running Python (most reliable)
    current_python = sys.executable
    if 'QGIS' in current_python:
        found_paths['python'] = current_python
    else:
        found_paths['python'] = current_python  # Save current Python anyway
    
    # 2. Check various QGIS installation paths
    qgis_app_paths = [
        "/Applications/QGIS.app",
        "/Applications/QGIS-LTR.app",
        "/Applications/QGIS3.app",
        "/Applications/QGIS-3.*.app",
        os.path.expanduser("~/Applications/QGIS.app"),
        "/opt/homebrew/Caskroom/qgis/*/QGIS.app",
        "/usr/local/Caskroom/qgis/*/QGIS.app",
    ]
    
    # Process glob patterns
    expanded_paths = []
    for path in qgis_app_paths:
        if '*' in path:
            expanded_paths.extend(glob.glob(path))
        else:
            expanded_paths.append(path)
    
    # Find existing QGIS.app
    for app_path in expanded_paths:
        if os.path.exists(app_path):
            found_paths['qgis_app'] = app_path
            
            # Python path candidates
            python_candidates = [
                f"{app_path}/Contents/MacOS/bin/python3",
                f"{app_path}/Contents/MacOS/bin/python",
                f"{app_path}/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
            ]
            
            for py_path in python_candidates:
                if os.path.exists(py_path):
                    found_paths['python'] = py_path
                    break
            
            # pip path candidates
            pip_candidates = [
                f"{app_path}/Contents/MacOS/bin/pip3",
                f"{app_path}/Contents/MacOS/bin/pip",
                f"{app_path}/Contents/Frameworks/Python.framework/Versions/Current/bin/pip3",
            ]
            
            for pip_path in pip_candidates:
                if os.path.exists(pip_path):
                    found_paths['pip'] = pip_path
                    break
            
            if found_paths['python']:
                break
    
    # 3. Find pip based on current Python
    if found_paths['python'] and not found_paths['pip']:
        python_dir = os.path.dirname(found_paths['python'])
        possible_pips = [
            os.path.join(python_dir, 'pip3'),
            os.path.join(python_dir, 'pip'),
        ]
        for pip_path in possible_pips:
            if os.path.exists(pip_path):
                found_paths['pip'] = pip_path
                break
    
    return found_paths

def install_requirements_direct_macos(requirements_path):
    """Install packages directly using Python import on macOS"""
    
    # Method 1: Direct pip import
    try:
        import pip
        pip.main(['install', '--user', '-r', requirements_path])
        return True
    except:
        pass
    
    # Method 2: Use pip._internal
    try:
        from pip._internal import main as pip_main
        pip_main(['install', '--user', '-r', requirements_path])
        return True
    except:
        pass
    
    # Method 3: Install packages individually using subprocess
    try:
        with open(requirements_path, 'r') as f:
            packages = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        failed_packages = []
        for package in packages:
            try:
                # Execute directly using exec
                cmd = f"""
import subprocess
result = subprocess.run(['{sys.executable}', '-m', 'pip', 'install', '--user', '{package}'], capture_output=True)
"""
                exec(cmd)
            except:
                failed_packages.append(package)
        
        if not failed_packages:
            return True
            
    except Exception as e:
        print(f"[QueryGIS] Direct installation error: {e}")
    
    return False

def create_installation_script_macos(plugin_dir, requirements_path):
    """Create installation script for macOS"""
    paths = find_qgis_python_macos()
    python_path = paths['python'] or sys.executable
    
    # .command script (double-clickable)
    script_path = os.path.join(plugin_dir, "install_packages.command")
    
    script_content = f"""#!/bin/bash
# QGIS Plugin Package Installation Script

echo "========================================="
echo "Installing QGIS QueryGIS Plugin packages"
echo "========================================="
echo ""

# Python path
PYTHON="{python_path}"

echo "Using Python: $PYTHON"
echo "Installing packages..."
echo ""

# Install packages
$PYTHON -m pip install --user -r "{requirements_path}"

if [ $? -eq 0 ]; then
    echo ""
    echo "Installation successful!"
    echo "Please restart QGIS."
else
    echo ""
    echo "Installation failed."
    echo ""
    echo "Try these commands manually:"
    echo "$PYTHON -m pip install --user pandas"
    echo "$PYTHON -m pip install --user numpy"
    echo "$PYTHON -m pip install --user plotly"
fi

echo ""
echo "Press Enter to close..."
read
"""
    
    with open(script_path, 'w') as f:
        f.write(script_content)
    
    os.chmod(script_path, 0o755)
    
    return script_path

def install_requirements_cross_platform():
    """Improved cross-platform installation"""
    plugin_dir = os.path.dirname(__file__)
    requirements_path = os.path.join(plugin_dir, "requirements.txt")
    
    if not os.path.exists(requirements_path):
        QMessageBox.warning(None, "Requirements Missing", "requirements.txt not found.")
        return False
    
    # Check required packages
    required_packages = []
    try:
        with open(requirements_path, 'r') as f:
            for line in f:
                if line.strip() and not line.startswith('#'):
                    pkg_name = line.split('==')[0].split('[')[0].strip()  # Handle extras
                    required_packages.append(pkg_name)
    except:
        required_packages = ['pandas', 'numpy', 'plotly']
    
    # Check already installed packages
    missing_packages = []
    for pkg in required_packages:
        try:
            __import__(pkg)
            print(f"[QueryGIS] {pkg} already installed")
        except ImportError:
            missing_packages.append(pkg)
    
    if not missing_packages:
        print("[QueryGIS] All packages already installed")
        return True
    
    print(f"[QueryGIS] Missing packages: {missing_packages}")
    
    # Progress dialog
    progress = QProgressDialog("Installing dependencies...", None, 0, 0)
    progress.setWindowModality(Qt.ApplicationModal)
    progress.setCancelButton(None)
    progress.setMinimumDuration(0)
    progress.setWindowTitle("Please Wait")
    progress.show()
    QApplication.processEvents()
    
    system = platform.system()
    
    if system == "Darwin":
        # macOS special handling
        progress.setLabelText("Attempting automatic installation...")
        QApplication.processEvents()
        
        # Step 1: Try direct installation
        if install_requirements_direct_macos(requirements_path):
            progress.close()
            QMessageBox.information(None, "Success", "Packages installed successfully!")
            prompt_restart()
            return True
        
        # Step 2: Create installation script
        script_path = create_installation_script_macos(plugin_dir, requirements_path)
        progress.close()
        
        # Step 3: Provide user options
        msg = QMessageBox()
        msg.setWindowTitle("Manual Installation Required")
        msg.setText(f"Automatic installation could not complete.\n"
                   f"Missing packages: {', '.join(missing_packages)}\n\n"
                   f"Choose an installation method:")
        
        terminal_btn = msg.addButton("Open in Terminal", QMessageBox.ActionRole)
        script_btn = msg.addButton("Run Install Script", QMessageBox.ActionRole)
        manual_btn = msg.addButton("Show Manual Commands", QMessageBox.ActionRole)
        cancel_btn = msg.addButton("Skip", QMessageBox.RejectRole)
        
        msg.exec_()
        
        clicked = msg.clickedButton()
        
        if clicked == terminal_btn:
            # Open Terminal and run command
            python_path = sys.executable
            cmd = f'cd "{plugin_dir}" && "{python_path}" -m pip install --user -r "{requirements_path}"'
            
            applescript = f'''
            tell application "Terminal"
                activate
                do script "{cmd}"
            end tell
            '''
            
            subprocess.run(['osascript', '-e', applescript])
            
        elif clicked == script_btn:
            # Run installation script
            subprocess.run(['open', script_path])
            
        elif clicked == manual_btn:
            # Show manual commands
            python_path = sys.executable
            commands = f"""Manual Installation Commands:

1. Open Terminal (Applications → Utilities → Terminal)

2. Copy and paste these commands one by one:

cd "{plugin_dir}"

"{python_path}" -m pip install --user pandas

"{python_path}" -m pip install --user numpy

"{python_path}" -m pip install --user plotly

Or all at once:
"{python_path}" -m pip install --user -r "{requirements_path}"

3. After installation completes, restart QGIS"""
            
            msg_box = QMessageBox()
            msg_box.setWindowTitle("Installation Commands")
            msg_box.setText("Copy these commands to install manually:")
            msg_box.setDetailedText(commands)
            msg_box.exec_()
    
    elif system == "Windows":
            from functools import partial
            
            process = QProcess()
            error_output_list = [] 
            
            def handle_stderr(proc_obj, log_list):
                raw_data = proc_obj.readAllStandardError().data()
                try:
                    decoded = raw_data.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = raw_data.decode("cp949", errors="replace")
                log_list.append(decoded)
            
            def install_finished(log_list, exit_code, exit_status):
                progress.close()
                
                if exit_code == 0:
                    QMessageBox.information(None, "Installation Complete", 
                                            "All required packages have been installed.")
                    prompt_restart()
                else:
                    error_message = ''.join(log_list).strip() or f"Exit code: {exit_code}"
                    QMessageBox.critical(None, "Installation Failed", error_message)
            
            process.readyReadStandardError.connect(partial(handle_stderr, process, error_output_list))
            process.finished.connect(partial(install_finished, error_output_list))
            
            qgis_path = str(os.path.dirname(sys.executable))
            bat_path = os.path.join(plugin_dir, "install_temp.bat")
            
            bat_content = f"""@echo off
    call "{qgis_path}\\o4w_env.bat"
    call py3_env
    python -m pip install -r "{requirements_path}"
    """
            with open(bat_path, "w") as f:
                f.write(bat_content)
            
            process.setProgram("cmd.exe")
            process.setArguments(["/C", bat_path])
            process.start()
        
    else:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", "-r", requirements_path],
                capture_output=True,
                text=True,
                timeout=300
            )
            progress.close()
            
            if result.returncode == 0:
                QMessageBox.information(None, "Installation Complete", 
                                       "All required packages have been installed.")
                prompt_restart()
            else:
                QMessageBox.critical(None, "Installation Failed", 
                                   f"Failed to install packages:\n{result.stderr}")
        except Exception as e:
            progress.close()
            QMessageBox.critical(None, "Installation Error", str(e))
    
    return True

def prompt_restart():
    """QGIS restart prompt"""
    msg = QMessageBox()
    msg.setWindowTitle("Restart Required")
    msg.setText("Required libraries have been successfully installed.\n\n"
                "QGIS must be restarted for the plugin to function properly.\n\n"
                "Restart QGIS now?")
    msg.setIcon(QMessageBox.Information)
    msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    
    if msg.exec_() != QMessageBox.Yes:
        return

    try:
        proj_path = QgsProject.instance().fileName() or ""
    except Exception:
        proj_path = ""

    system = platform.system()
    ok = False

    if system == "Windows":
        app_path = QCoreApplication.applicationFilePath() or ""
        candidates = []
        if app_path:
            p = Path(app_path)
            if p.exists() and p.suffix.lower() == ".exe":
                candidates.append(p)

        base = Path(sys.executable).parent.parent
        candidates += [
            base / "bin" / "qgis-bin.exe",
            base / "bin" / "qgis-ltr-bin.exe",
        ]
        exe = next((c for c in candidates if c and c.exists()), None)

        if exe:
            program = str(exe)
            workdir = str(exe.parent)
            args = [proj_path] if proj_path else []
            ok = QProcess.startDetached(program, args, workdir)

    elif system == "Darwin":
        qgis_apps = [
            "/Applications/QGIS.app",
            "/Applications/QGIS-LTR.app",
            "/Applications/QGIS3.app"
        ]
        
        qgis_app = None
        for app in qgis_apps:
            if Path(app).exists():
                qgis_app = app
                break
        
        if qgis_app:
            program = "/usr/bin/open"
            args = ["-n", "-a", qgis_app]
            if proj_path:
                args.extend(["--args", proj_path])
            ok = QProcess.startDetached(program, args)

    else:  # Linux
        app_path = QCoreApplication.applicationFilePath()
        if app_path and Path(app_path).exists():
            program = app_path
            args = [proj_path] if proj_path else []
            ok = QProcess.startDetached(program, args)
        else:
            program = "qgis"
            args = [proj_path] if proj_path else []
            ok = QProcess.startDetached(program, args)

    if not ok:
        QMessageBox.information(None, "Restart Required",
                              "Could not restart QGIS automatically.\n"
                              "Please close and restart QGIS manually.")
        return

    def _terminate():
        if platform.system() == "Windows":
            os._exit(0)
        else:
            QApplication.quit()

    QTimer.singleShot(800, _terminate)

def classFactory(iface):
    """Plugin entry point"""
    plugin_dir = os.path.dirname(__file__)
    flag_path = os.path.join(plugin_dir, ".installed")

    if platform.system() == "Darwin":
        print(f"[QueryGIS] Running on macOS")
        print(f"[QueryGIS] Python: {sys.executable}")
        print(f"[QueryGIS] Plugin dir: {plugin_dir}")

    if not os.path.exists(flag_path):
        try:
            success = install_requirements_cross_platform()
            if success:
                with open(flag_path, "w") as f:
                    f.write("installed")
        except Exception as e:
            print(f"[QueryGIS] Installation error: {e}")
            # Try to load plugin even if installation fails
            with open(flag_path, "w") as f:
                f.write("manual_install_needed")

    try:
        from .query_gis import QueryGIS
        return QueryGIS(iface)
    except ImportError as e:
        import traceback
        error_msg = f"Failed to load plugin:\n{str(e)}\n\n"
        
        if platform.system() == "Darwin":
            requirements_path = os.path.join(plugin_dir, "requirements.txt")
            error_msg += f"Please install required packages manually:\n\n"
            error_msg += f"1. Open Terminal\n"
            error_msg += f"2. Run: {sys.executable} -m pip install --user -r \"{requirements_path}\"\n"
        
        error_msg += f"\nDetails:\n{traceback.format_exc()}"
        
        QMessageBox.critical(None, "Plugin Load Error", error_msg)
        
        class DummyPlugin:
            def initGui(self): pass
            def unload(self): pass
        return DummyPlugin()