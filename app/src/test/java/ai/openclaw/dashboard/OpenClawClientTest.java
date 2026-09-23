package ai.openclaw.dashboard;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class OpenClawClientTest {
    @Test
    public void controlUiGatewayUrlUsesWebSocketScheme() {
        assertEquals(
                "wss://gateway.example/",
                GatewayUrls.toWebSocket("https://gateway.example/"));
        assertEquals(
                "ws://127.0.0.1:18789/",
                GatewayUrls.toWebSocket("http://127.0.0.1:18789/"));
    }

    @Test
    public void nativeControlAuthOnlyOverridesBrowserBootstrapWithCredentials() {
        assertTrue(GatewayUrls.hasExplicitCredentials("token", ""));
        assertTrue(GatewayUrls.hasExplicitCredentials("", "password"));
        assertTrue(!GatewayUrls.hasExplicitCredentials("", ""));
        assertTrue(!GatewayUrls.hasExplicitCredentials(null, null));
    }

    @Test
    public void nodeCapabilitiesAdvertiseMobileUi() {
        boolean found = false;
        for (String capability : OpenClawClient.nodeCapabilityNames()) {
            if ("mobileUI".equals(capability)) {
                found = true;
                break;
            }
        }
        assertTrue("mobileUI capability must accompany mobile.ui commands", found);
    }
}
