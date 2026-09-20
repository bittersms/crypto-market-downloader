#!/usr/bin/env python3
"""
Crypto Market Data Downloader - Launcher

Run this script to start the application.
"""
import sys
import os

# Add the app directory to the path
app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, app_dir)

# Ensure database is initialized
from database import init_db
init_db()

# Start the GUI
import tkinter as tk
from app import CryptoMarketDownloaderApp

if __name__ == "__main__":
    root = tk.Tk()
    app = CryptoMarketDownloaderApp(root)
    root.mainloop()
