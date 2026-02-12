# Discord to Revolt Migrator

A Python tool to migrate a Discord Server Template to a Revolt/Stoat Server. It supports "Smart Matching" to reuse existing channels, preventing duplication and preserving your new server structure.

## 🌟 Features
* **Smart Mapping**: Automatically detects existing channels (even if names have different emojis or capitalization) and links them instead of creating duplicates.
* **Fuzzy Matching**: Matches `⚙️Channel⚙️` (Discord) to `⚙️Channel⚙️` (Revolt/Stoat).
* **Role Migration**: Imports roles, colors, and permissions.
* **Category Support**: Creates categories and sorts channels into them.
* **Permission Overwrites**: Applies complex channel permissions (deny/allow rules).
* **Limit Handling**: Detects the Revolt 200-channel limit and gracefully skips extras.
* **.env Support**: Load credentials from a file for easy re-running.

## 🛠️ Prerequisites
* Python 3.10 or higher
* A Revolt/Stoat Account and Server
* A Revolt/Stoat Bot (Create this from Settings -> My Bots -> Create Bot)

## 📦 Installation

1.  Clone the repository or download the files.
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

## ⚙️ Configuration

1.  Rename `.env.example` to `.env`.
2.  Fill in the required fields:

    ```ini
    # Discord Template URL (e.g. [https://discord.new/AbCdEfGhIjK](https://discord.new/AbCdEfGhIjK))
    DISCORD_TEMPLATE_URL=[https://discord.new/YOUR_CODE_HERE](https://discord.new/YOUR_CODE_HERE)

    # Your Revolt Bot Token
    REVOLT_BOT_TOKEN=your_bot_token_here

    # The ID of the Revolt Server you are importing into
    REVOLT_SERVER_ID=your_server_id_here
    ```

## 🚀 Usage

Run the script:

```bash
python main.py