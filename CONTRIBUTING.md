# Contributing to Indian SIM SMS Gateway

Thank you for your interest in contributing to the **Indian SIM SMS Gateway**! This project aims to build a robust, self-healing bridge between Indian SIM networks and global infrastructure.

We welcome contributions of all forms: bug fixes, new features, documentation improvements, and hardware compatibility tests.

---

## 🛠️ Development Setup

### 1. Hardware Requirements
*   **ESP32 DevKit V1** (or compatible ESP32 board)
*   **SIM800L / A7670C GSM Module**
*   Active Indian SIM card with SMS pack

### 2. Firmware (C++)
We use the Arduino framework with PlatformIO or Arduino IDE.

1.  **Install dependencies**:
    *   `TinyGSM`
    *   `PubSubClient`
    *   `ArduinoJson`
2.  **Configuration**:
    *   Copy `config.example.h` to `config.h`.
    *   Set your WiFi credentials and MQTT broker details.
3.  **Build & Flash**:
    ```bash
    # Using Arduino CLI
    arduino-cli compile --fqbn esp32:esp32:esp32doit-devkit-v1 firmware/esp32_sms_gateway
    arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32doit-devkit-v1 firmware/esp32_sms_gateway
    ```

### 3. Backend (Python/FastAPI)
We use Python 3.10+ and Docker.

1.  **Virtual Environment**:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```
2.  **Environment Variables**:
    *   Copy `.env.example` to `.env`.
    *   Fill in `TELEGRAM_BOT_TOKEN`, `REDIS_URL`, etc.
3.  **Run Locally**:
    ```bash
    python main.py
    # API available at http://localhost:8000
    ```
4.  **Run Tests**:
    ```bash
    pytest tests/
    ```

---

## 📏 Coding Standards

### Python
*   Follow **PEP 8** style guidelines.
*   Use **Type Hints** (`typing`) for all function arguments and return values.
*   We use `pytest` for testing. Ensure all tests pass before submitting a PR.
*   **Asyncio**: Use `async/await` patterns for all I/O bound operations (Redis, HTTP requests).

### C++ (Firmware)
*   Prioritize memory safety and stability.
*   Avoid using `String` class where possible; prefer `char[]` to prevent heap fragmentation.
*   Comment complex logic, especially around NVS storage and Watchdog timers.

---

## 🚀 Pull Request Process

1.  **Fork** the repository and create your branch from `main`.
2.  **Test** your changes.
    *   For backend changes: Run `pytest`.
    *   For firmware changes: Verify on actual hardware if possible, or clearly state "Hardware verification needed".
3.  **Update Documentation**: If you changed APIs or hardware wiring, update `README.md` and `docs/`.
4.  **Submit PR**: Provide a clear description of the problem and any relevant context.

---

## 🐛 Reporting Bugs

Please check existing issues before ensuring a new one. Include:
*   **Component**: (Firmware, Backend, or Docs)
*   **Logs**: relevant serial logs or backend tracebacks.
*   **Steps to Reproduce**.

---

Happy Coding! 🚀
