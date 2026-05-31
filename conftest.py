# conftest.py — repo-root marker so pytest puts the project root on sys.path,
# letting tests import the top-level modules (agent, vault.*, cal.*, mail.*)
# without an installed package. Intentionally empty otherwise.
