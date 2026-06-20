(function () {
    const localApiUrl = "http://localhost:8001";
    const isLocalPage = ["localhost", "127.0.0.1", ""].includes(window.location.hostname);
    const configuredApiUrl =
        window.PROTOCOLSNERD_API_URL ||
        window.PROTOCOLSNERD_BACKEND_URL ||
        (isLocalPage ? null : localStorage.getItem("PROTOCOLSNERD_API_URL")) ||
        localApiUrl;

    window.env = {
        FRONTEND_FLOW: {
            SITE_NAME: "ProtocolsNerd",
            SITE_LOGO: "assets/protocols_nerd_default_logo.png",
            SITE_ICON: "CN",
            SITE_TAGLINE: "Local AI document analysis with agentic and prompt-based execution modes.",
            DISCLAIMER: "This tool performs local document analysis using Ollama. No data leaves your machine. Results should be reviewed by a qualified professional.",
            QUESTION_PLACEHOLDER: "Example: Check whether this interconnection agreement complies with the uploaded regulations.",
            STYLES: {
                BACKGROUND_COLOR: "#EFF8FF",
                FONT_FAMILY: "'Roboto', sans-serif",
                SUBMIT_BUTTON_BG: "#007bff"
            },
            API_URL: configuredApiUrl
        }
    };
})();
