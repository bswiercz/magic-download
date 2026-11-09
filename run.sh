#!/bin/bash

echo -n "Enter session cookie: "
read session

urls=(
# ENTER URLS TO DOWNLOAD HERE
)

for url in "${urls[@]}"; do
  echo "[+] Processing: $url"
  ./run.py "$url" "$session"
done

echo "[+] All URLs processed"
