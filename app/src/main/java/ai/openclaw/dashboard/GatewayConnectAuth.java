package ai.openclaw.dashboard;

import java.util.Locale;

/** Selects Gateway connect credentials using the same precedence as OpenClaw's native clients. */
final class GatewayConnectAuth {
    private GatewayConnectAuth() {}

    static Selection select(
            String configuredToken,
            String bootstrapToken,
            String storedDeviceToken,
            String password
    ) {
        String token = normalize(configuredToken);
        String bootstrap = normalize(bootstrapToken);
        String deviceToken = normalize(storedDeviceToken);
        String normalizedPassword = normalize(password);

        boolean useDeviceToken = token == null && normalizedPassword == null && deviceToken != null;
        String selectedDeviceToken = useDeviceToken ? deviceToken : null;
        String selectedBootstrap = token == null && normalizedPassword == null && selectedDeviceToken == null
                ? bootstrap
                : null;
        String signatureToken = token != null
                ? token
                : selectedDeviceToken != null ? selectedDeviceToken : selectedBootstrap;

        return new Selection(token, selectedBootstrap, selectedDeviceToken, normalizedPassword, signatureToken);
    }

    static boolean shouldClearStoredDeviceToken(
            String error,
            boolean hasStoredDeviceToken,
            boolean hasFallbackCredential
    ) {
        if (!hasStoredDeviceToken || !hasFallbackCredential || error == null) return false;
        String normalized = error.toLowerCase(Locale.ROOT);
        return normalized.contains("device token mismatch")
                || normalized.contains("rotate/reissue device token")
                || normalized.contains("invalid device token")
                || normalized.contains("expired device token")
                || normalized.contains("device signature invalid")
                || normalized.contains("device-signature");
    }

    private static String normalize(String value) {
        if (value == null) return null;
        String trimmed = value.trim();
        return trimmed.isEmpty() ? null : trimmed;
    }

    static final class Selection {
        final String token;
        final String bootstrapToken;
        final String deviceToken;
        final String password;
        final String signatureToken;

        Selection(String token, String bootstrapToken, String deviceToken, String password, String signatureToken) {
            this.token = token;
            this.bootstrapToken = bootstrapToken;
            this.deviceToken = deviceToken;
            this.password = password;
            this.signatureToken = signatureToken;
        }

        boolean usesStoredDeviceToken() {
            return deviceToken != null;
        }
    }
}
