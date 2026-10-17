#!/bin/zsh
cd "${0:A:h}" || exit 1

echo "Tell Me Why — YouTube Reauthorization"
echo "======================================"
echo

if [[ ! -x .venv/bin/python ]]; then
  echo "Project virtual environment not found at .venv/bin/python."
  status=1
elif ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is not installed. Install it with: brew install gh"
  status=1
elif ! gh auth status --hostname github.com >/dev/null 2>&1; then
  echo "GitHub CLI is not signed in. Run: gh auth login"
  status=1
else
  if ! .venv/bin/python -c 'import google_auth_oauthlib' >/dev/null 2>&1; then
    echo "Installing the local Google OAuth helper…"
    .venv/bin/python -m pip install 'google-auth-oauthlib>=1,<2'
  fi
  echo
  .venv/bin/python -m automation.reauthorize_youtube
  status=$?
fi

echo
if [[ ${status:-1} -eq 0 ]]; then
  echo "Finished successfully."
else
  echo "Reauthorization did not finish. Read the message above; no workflow was started."
fi
echo
read "?Press Return to close this window."
exit ${status:-1}
