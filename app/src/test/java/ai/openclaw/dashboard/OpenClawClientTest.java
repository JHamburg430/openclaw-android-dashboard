package ai.openclaw.dashboard;

import org.junit.Test;

import static org.junit.Assert.assertTrue;

public class OpenClawClientTest {
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
