package ai.openclaw.dashboard;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.os.Bundle;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.List;

public final class PhoneAccessibilityService extends AccessibilityService {
    private static volatile PhoneAccessibilityService instance;

    @Override protected void onServiceConnected() { instance = this; }
    @Override public void onAccessibilityEvent(AccessibilityEvent event) { }
    @Override public void onInterrupt() { }
    @Override public void onDestroy() { if (instance == this) instance = null; super.onDestroy(); }

    static boolean isConnected() { return instance != null; }

    static JSONObject snapshot(int limit) throws Exception {
        PhoneAccessibilityService service = requireService();
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        JSONArray nodes = new JSONArray();
        if (root != null) {
            ArrayDeque<AccessibilityNodeInfo> queue = new ArrayDeque<>();
            queue.add(root);
            while (!queue.isEmpty() && nodes.length() < limit) {
                AccessibilityNodeInfo node = queue.removeFirst();
                CharSequence text = node.getText();
                CharSequence description = node.getContentDescription();
                if ((text != null && text.length() > 0) || (description != null && description.length() > 0) || node.isClickable() || node.isEditable()) {
                    nodes.put(new JSONObject()
                            .put("text", text == null ? "" : text.toString())
                            .put("description", description == null ? "" : description.toString())
                            .put("viewId", node.getViewIdResourceName() == null ? "" : node.getViewIdResourceName())
                            .put("className", node.getClassName() == null ? "" : node.getClassName().toString())
                            .put("clickable", node.isClickable())
                            .put("editable", node.isEditable())
                            .put("enabled", node.isEnabled()));
                }
                for (int i = 0; i < node.getChildCount(); i++) {
                    AccessibilityNodeInfo child = node.getChild(i);
                    if (child != null) queue.addLast(child);
                }
            }
        }
        return new JSONObject()
                .put("packageName", root == null || root.getPackageName() == null ? "" : root.getPackageName().toString())
                .put("nodes", nodes)
                .put("count", nodes.length());
    }

    static JSONObject perform(JSONObject params) throws Exception {
        PhoneAccessibilityService service = requireService();
        String action = params.optString("action", "");
        if ("back".equals(action)) return global(service, GLOBAL_ACTION_BACK, action);
        if ("home".equals(action)) return global(service, GLOBAL_ACTION_HOME, action);
        if ("recents".equals(action)) return global(service, GLOBAL_ACTION_RECENTS, action);
        if ("notifications".equals(action)) return global(service, GLOBAL_ACTION_NOTIFICATIONS, action);
        if ("tap".equals(action)) return tap(service, params.optDouble("x", -1), params.optDouble("y", -1));

        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        if (root == null) throw new IllegalStateException("No active app window is available.");
        AccessibilityNodeInfo target = findNode(root, params.optString("viewId", ""), params.optString("text", ""));
        if (target == null) throw new IllegalArgumentException("Matching UI element was not found.");
        if ("click".equals(action)) {
            AccessibilityNodeInfo clickable = target;
            while (clickable != null && !clickable.isClickable()) clickable = clickable.getParent();
            if (clickable == null || !clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)) throw new IllegalStateException("UI element could not be clicked.");
        } else if ("setText".equals(action)) {
            Bundle arguments = new Bundle();
            arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, params.optString("value", ""));
            if (!target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) throw new IllegalStateException("Text could not be set.");
        } else if ("scrollForward".equals(action)) {
            if (!target.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)) throw new IllegalStateException("Element could not scroll forward.");
        } else if ("scrollBackward".equals(action)) {
            if (!target.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)) throw new IllegalStateException("Element could not scroll backward.");
        } else {
            throw new IllegalArgumentException("Unsupported accessibility action.");
        }
        return new JSONObject().put("performed", true).put("action", action);
    }

    private static JSONObject global(PhoneAccessibilityService service, int value, String name) throws Exception {
        if (!service.performGlobalAction(value)) throw new IllegalStateException("Global action was rejected.");
        return new JSONObject().put("performed", true).put("action", name);
    }

    private static JSONObject tap(PhoneAccessibilityService service, double x, double y) throws Exception {
        if (x < 0 || y < 0) throw new IllegalArgumentException("x and y are required for tap.");
        Path path = new Path();
        path.moveTo((float) x, (float) y);
        GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, 80)).build();
        if (!service.dispatchGesture(gesture, null, null)) throw new IllegalStateException("Tap gesture was rejected.");
        return new JSONObject().put("performed", true).put("action", "tap").put("x", x).put("y", y);
    }

    private static AccessibilityNodeInfo findNode(AccessibilityNodeInfo root, String viewId, String text) {
        if (!viewId.isEmpty()) {
            List<AccessibilityNodeInfo> matches = root.findAccessibilityNodeInfosByViewId(viewId);
            if (matches != null && !matches.isEmpty()) return matches.get(0);
        }
        if (!text.isEmpty()) {
            List<AccessibilityNodeInfo> matches = root.findAccessibilityNodeInfosByText(text);
            if (matches != null && !matches.isEmpty()) return matches.get(0);
        }
        return null;
    }

    private static PhoneAccessibilityService requireService() {
        PhoneAccessibilityService service = instance;
        if (service == null) throw new IllegalStateException("Accessibility Control is not enabled or connected.");
        return service;
    }
}
