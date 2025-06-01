# AI Companion Bot

Welcome to the AI Companion Bot project!

This project aims to create an engaging and interactive AI companion with multiple personalities and a subscription-based model.

For detailed documentation, please see the [docs](docs/) directory.

## Key Features

- Multiple, distinct AI personas (e.g., Aeris, Luneth)
- Subscription system with different tiers and features
- Intelligent and context-aware dialogues
- Personalized memory for each user
- Interactive elements like quick actions and dynamic text
- Notification system for updates and marketing
- Flexible and extensible architecture

## Getting Started

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/ai-companion-bot.git
    cd ai-companion-bot
    ```

2.  **Set up a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows use `venv\Scripts\activate`
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    pip install -r requirements-dev.txt # For development
    ```

4.  **Configure environment variables:**
    Copy `.env.example` to `.env` and fill in your API keys and other settings.
    ```bash
    cp .env.example .env
    ```

5.  **Run the bot:**
    ```bash
    python main.py
    ```

## Project Structure

```
.
├── .github/                # GitHub Actions workflows
├── .gitignore              # Specifies intentionally untracked files that Git should ignore
├── .env.example            # Example environment variables file
├── CODE_OF_CONDUCT.md      # Code of conduct for contributors (now in /docs)
├── CONTRIBUTING.md         # Guidelines for contributing (now in /docs)
├── DEPLOYMENT_GUIDE.md     # Instructions for deploying the bot (now in /docs)
├── LICENSE                 # Project license (MIT)
├── MEMORY_BANK_DESIGN.md   # Design of the memory bank system (now in /docs)
├── PROJECT_OVERVIEW.md     # High-level project overview (now in /docs)
├── README.md               # This file - basic project information
├── TECHNICAL_SPECIFICATION.md # Detailed technical specifications (now in /docs)
├── analytics/              # Modules for business intelligence and ML predictions
├── config/                 # Configuration files (settings, prompts)
├── database/               # Database models, enums, and operations
├── docs/                   # Detailed project documentation (moved .md files here)
├── fsm/                    # Finite State Machine definitions
├── handlers/               # Telegram bot command and message handlers
├── main.py                 # Main application entry point
├── monitoring/             # Monitoring and alerting systems
├── optimization/           # Business process optimization modules
├── personas/               # Persona definition files (e.g., aeris.txt)
├── production_validation.py # Scripts for production validation
├── reporting/              # Executive reporting modules
├── requirements-dev.txt    # Development dependencies
├── requirements.txt        # Project dependencies
├── rules/                  # Project-specific rules and guidelines
├── services/               # Core application services (LLM, TTS, payments, etc.)
├── tests/                  # Unit and integration tests
└── utils/                  # Utility functions and helper modules
```

## Contributing

Please see the [CONTRIBUTING.md](docs/CONTRIBUTING.md) file in the `docs` directory for details on how to contribute to this project.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.