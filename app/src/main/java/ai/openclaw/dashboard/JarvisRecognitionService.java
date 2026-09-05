package ai.openclaw.dashboard;

import android.content.Intent;
import android.os.Bundle;
import android.speech.RecognitionService;

public final class JarvisRecognitionService extends RecognitionService {
    @Override protected void onStartListening(Intent recognizerIntent, Callback listener) {
        try {
            listener.error(android.speech.SpeechRecognizer.ERROR_CLIENT);
        } catch (android.os.RemoteException ignored) { }
    }
    @Override protected void onCancel(Callback listener) { }
    @Override protected void onStopListening(Callback listener) { }
}
