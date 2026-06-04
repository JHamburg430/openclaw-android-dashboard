package ai.openclaw.dashboard;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.Signature;
import java.security.interfaces.EdECPublicKey;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.Arrays;

final class IdentityStore {
    private static final byte[] ED25519_SPKI_PREFIX = hex("302a300506032b6570032100");
    private static final String PREFS = "openclaw_identity";

    private final SharedPreferences prefs;

    IdentityStore(Context context) {
        this.prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    synchronized Identity loadOrCreate() throws Exception {
        String publicDer = prefs.getString("publicDer", null);
        String privateDer = prefs.getString("privateDer", null);
        if (publicDer != null && privateDer != null) {
            KeyFactory factory = KeyFactory.getInstance("Ed25519");
            PublicKey publicKey = factory.generatePublic(new X509EncodedKeySpec(fromB64(publicDer)));
            PrivateKey privateKey = factory.generatePrivate(new PKCS8EncodedKeySpec(fromB64(privateDer)));
            return build(publicKey, privateKey);
        }

        KeyPairGenerator generator = KeyPairGenerator.getInstance("Ed25519");
        KeyPair pair = generator.generateKeyPair();
        prefs.edit()
                .putString("publicDer", toB64(pair.getPublic().getEncoded()))
                .putString("privateDer", toB64(pair.getPrivate().getEncoded()))
                .apply();
        return build(pair.getPublic(), pair.getPrivate());
    }

    String getDeviceToken() {
        return prefs.getString("deviceToken", null);
    }

    String getDeviceTokenScopesJson() {
        return prefs.getString("deviceTokenScopes", "[]");
    }

    void saveDeviceToken(String token, String scopesJson) {
        prefs.edit()
                .putString("deviceToken", token)
                .putString("deviceTokenScopes", scopesJson == null ? "[]" : scopesJson)
                .apply();
    }

    void clearDeviceToken() {
        prefs.edit().remove("deviceToken").remove("deviceTokenScopes").apply();
    }

    private static Identity build(PublicKey publicKey, PrivateKey privateKey) throws Exception {
        byte[] rawPublic = rawEd25519Public(publicKey.getEncoded());
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(rawPublic);
        return new Identity(hexLower(digest), b64Url(rawPublic), publicKey, privateKey);
    }

    static String sign(PrivateKey privateKey, String payload) throws Exception {
        Signature signature = Signature.getInstance("Ed25519");
        signature.initSign(privateKey);
        signature.update(payload.getBytes(StandardCharsets.UTF_8));
        return b64Url(signature.sign());
    }

    private static byte[] rawEd25519Public(byte[] spki) {
        if (spki.length == ED25519_SPKI_PREFIX.length + 32) {
            byte[] prefix = Arrays.copyOfRange(spki, 0, ED25519_SPKI_PREFIX.length);
            if (Arrays.equals(prefix, ED25519_SPKI_PREFIX)) {
                return Arrays.copyOfRange(spki, ED25519_SPKI_PREFIX.length, spki.length);
            }
        }
        return spki;
    }

    static Setup parseSetupCode(String input) throws Exception {
        String trimmed = input == null ? "" : input.trim();
        if (trimmed.isEmpty()) throw new IllegalArgumentException("Setup code is empty.");
        String decoded = new String(Base64.decode(trimmed, Base64.URL_SAFE | Base64.NO_WRAP), StandardCharsets.UTF_8);
        JSONObject json = new JSONObject(decoded);
        return new Setup(
                json.optString("url", ""),
                json.optString("publicUrl", ""),
                json.optString("bootstrapToken", "")
        );
    }

    private static String toB64(byte[] bytes) {
        return Base64.encodeToString(bytes, Base64.NO_WRAP);
    }

    private static byte[] fromB64(String input) {
        return Base64.decode(input, Base64.NO_WRAP);
    }

    static String b64Url(byte[] bytes) {
        return Base64.encodeToString(bytes, Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
    }

    private static String hexLower(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) sb.append(String.format("%02x", b & 0xff));
        return sb.toString();
    }

    private static byte[] hex(String text) {
        byte[] out = new byte[text.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(text.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    static final class Identity {
        final String deviceId;
        final String publicKeyRawBase64Url;
        final PublicKey publicKey;
        final PrivateKey privateKey;

        Identity(String deviceId, String publicKeyRawBase64Url, PublicKey publicKey, PrivateKey privateKey) {
            this.deviceId = deviceId;
            this.publicKeyRawBase64Url = publicKeyRawBase64Url;
            this.publicKey = publicKey;
            this.privateKey = privateKey;
        }
    }

    static final class Setup {
        final String url;
        final String publicUrl;
        final String bootstrapToken;

        Setup(String url, String publicUrl, String bootstrapToken) {
            this.url = url;
            this.publicUrl = publicUrl;
            this.bootstrapToken = bootstrapToken;
        }
    }
}
