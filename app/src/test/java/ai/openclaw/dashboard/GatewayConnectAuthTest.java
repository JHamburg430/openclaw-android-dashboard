package ai.openclaw.dashboard;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class GatewayConnectAuthTest {
    @Test
    public void passwordDoesNotSignWithStoredDeviceToken() {
        GatewayConnectAuth.Selection auth = GatewayConnectAuth.select(null, null, "stored-device", "gateway-password");

        assertEquals("gateway-password", auth.password);
        assertNull(auth.deviceToken);
        assertNull(auth.signatureToken);
        assertFalse(auth.usesStoredDeviceToken());
    }

    @Test
    public void configuredTokenWinsOverStoredDeviceToken() {
        GatewayConnectAuth.Selection auth = GatewayConnectAuth.select("configured-token", null, "stored-device", null);

        assertEquals("configured-token", auth.token);
        assertEquals("configured-token", auth.signatureToken);
        assertNull(auth.deviceToken);
    }

    @Test
    public void storedDeviceTokenIsUsedOnlyWithoutExplicitAuthentication() {
        GatewayConnectAuth.Selection auth = GatewayConnectAuth.select(null, null, "stored-device", null);

        assertEquals("stored-device", auth.deviceToken);
        assertEquals("stored-device", auth.signatureToken);
        assertTrue(auth.usesStoredDeviceToken());
    }

    @Test
    public void bootstrapTokenIsUsedWhenNoOtherCredentialExists() {
        GatewayConnectAuth.Selection auth = GatewayConnectAuth.select(null, "bootstrap", null, null);

        assertEquals("bootstrap", auth.bootstrapToken);
        assertEquals("bootstrap", auth.signatureToken);
    }

    @Test
    public void signatureFailureClearsStoredTokenOnlyWhenFallbackExists() {
        assertTrue(GatewayConnectAuth.shouldClearStoredDeviceToken(
                "device signature invalid", true, true));
        assertFalse(GatewayConnectAuth.shouldClearStoredDeviceToken(
                "device signature invalid", true, false));
        assertFalse(GatewayConnectAuth.shouldClearStoredDeviceToken(
                "device signature invalid", false, true));
    }
}
