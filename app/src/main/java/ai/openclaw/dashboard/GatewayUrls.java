package ai.openclaw.dashboard;

final class GatewayUrls {
    private GatewayUrls() {}

    static boolean hasExplicitCredentials(String token, String password) {
        return token != null && !token.trim().isEmpty()
                || password != null && !password.trim().isEmpty();
    }

    static String toWebSocket(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) throw new IllegalStateException("Gateway URL is required.");
        if (value.startsWith("https://")) value = "wss://" + value.substring(8);
        else if (value.startsWith("http://")) value = "ws://" + value.substring(7);
        else if (!value.startsWith("ws://") && !value.startsWith("wss://")) value = "wss://" + value;
        if (!value.endsWith("/")) value = value + "/";
        return value;
    }
}
