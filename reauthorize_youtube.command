#!/bin/zsh
cd "${0:A:h}" || exit 1

AUTH_VENV=".youtube-auth-venv"
AUTH_PYTHON="$AUTH_VENV/bin/python"
result_code=0

echo "Tell Me Why — YouTube Reauthorization"
echo "======================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it with: brew install python"
  result_code=1
elif [[ ! -x "$AUTH_PYTHON" ]] || ! "$AUTH_PYTHON" -c 'import sys' >/dev/null 2>&1; then
  echo "Creating the YouTube authorization environment…"
  python3 -m venv --clear "$AUTH_VENV"
  result_code=$?
fi

if [[ $result_code -eq 0 ]] && ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is not installed. Install it with: brew install gh"
  result_code=1
elif [[ $result_code -eq 0 ]] && ! gh auth status --hostname github.com >/dev/null 2>&1; then
  echo "GitHub CLI is not signed in. Run: gh auth login"
  result_code=1
fi

if [[ $result_code -eq 0 ]] && ! "$AUTH_PYTHON" -c 'import google_auth_oauthlib, requests' >/dev/null 2>&1; then
  echo "Installing the YouTube authorization helpers…"
  "$AUTH_PYTHON" -m pip install 'google-auth-oauthlib>=1,<2' 'requests>=2.32,<3'
  result_code=$?
fi

if [[ $result_code -eq 0 ]]; then
  echo
  "$AUTH_PYTHON" -m automation.reauthorize_youtube
  result_code=$?
fi

echo
if [[ $result_code -eq 0 ]]; then
  echo "Finished successfully."
else
  echo "Reauthorization did not finish. Read the message above; no workflow was started."
fi
echo
read "?Press Return to close this window."
exit $result_code
