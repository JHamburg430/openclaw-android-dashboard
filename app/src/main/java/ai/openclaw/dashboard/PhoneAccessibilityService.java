package ai.openclaw.dashboard;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;

public final class PhoneAccessibilityService extends AccessibilityService {
    private static volatile PhoneAccessibilityService instance;
    private static volatile SnapshotState latestSnapshot;

    @Override protected void onServiceConnected() { instance = this; }
    @Override public void onAccessibilityEvent(AccessibilityEvent event) { }
    @Override public void onInterrupt() { }
    @Override public void onDestroy() {
        if (instance == this) instance = null;
        latestSnapshot = null;
        super.onDestroy();
    }

    static boolean isConnected() { return instance != null; }

    static synchronized JSONObject snapshot(int limit) throws Exception {
        PhoneAccessibilityService service = requireService();
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        String snapshotId = UUID.randomUUID().toString();
        JSONArray nodes = new JSONArray();
        Map<String, String> pathsByRef = new HashMap<>();
        String packageName = root == null || root.getPackageName() == null
                ? "" : root.getPackageName().toString();
        String windowTitle = "";
        if (root != null && root.getWindow() != null && root.getWindow().getTitle() != null) {
            windowTitle = root.getWindow().getTitle().toString();
        }

        if (root != null) {
            ArrayDeque<PendingNode> queue = new ArrayDeque<>();
            queue.add(new PendingNode(root, "", null));
            int refIndex = 0;
            while (!queue.isEmpty() && nodes.length() < limit) {
                PendingNode pending = queue.removeFirst();
                AccessibilityNodeInfo node = pending.node;
                boolean include = isObservable(node);
                String ref = include ? "n" + refIndex++ : pending.parentRef;
                if (include) {
                    pathsByRef.put(ref, pending.path);
                    nodes.put(semanticNode(node, ref, pending.parentRef));
                }
                for (int index = 0; index < node.getChildCount(); index++) {
                    AccessibilityNodeInfo child = node.getChild(index);
                    if (child != null) {
                        String childPath = pending.path.isEmpty()
                                ? Integer.toString(index)
                                : pending.path + "/" + index;
                        queue.addLast(new PendingNode(child, childPath, ref));
                    }
                }
            }
        }

        latestSnapshot = new SnapshotState(snapshotId, packageName, pathsByRef);
        return new JSONObject()
                .put("snapshotId", snapshotId)
                .put("package", packageName)
                .put("windowTitle", windowTitle)
                .put("nodes", nodes);
    }

    static synchronized JSONObject perform(JSONObject params) throws Exception {
        JSONObject action = params.optJSONObject("action");
        if (action == null) return performLegacy(params);

        PhoneAccessibilityService service = requireService();
        SnapshotState snapshot = latestSnapshot;
        String snapshotId = params.optString("snapshotId", "");
        if (snapshot == null || snapshotId.isEmpty() || !snapshot.snapshotId.equals(snapshotId)) {
            return outcome("target_stale", "The accessibility snapshot is no longer current.");
        }

        String type = action.optString("type", "");
        if ("global_action".equals(type)) return global(service, action.optString("name", ""));
        if ("tap".equals(type)) {
            return gesture(service, action.optDouble("x", -1), action.optDouble("y", -1),
                    action.optDouble("x", -1), action.optDouble("y", -1), 80);
        }
        if ("swipe".equals(type)) {
            return gesture(service,
                    action.optDouble("x1", -1), action.optDouble("y1", -1),
                    action.optDouble("x2", -1), action.optDouble("y2", -1),
                    action.optLong("durationMs", 250));
        }
        if ("wait".equals(type)) {
            SystemClock.sleep(Math.max(0, action.optLong("ms", 0)));
            return outcome("ok", "Wait completed.");
        }

        String ref = action.optString("ref", "");
        String path = snapshot.pathsByRef.get(ref);
        if (path == null) return outcome("target_not_found", "The requested element is not in the latest snapshot.");
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        if (root == null) return outcome("secure_content", "The active window is unavailable to Accessibility Control.");
        String currentPackage = root.getPackageName() == null ? "" : root.getPackageName().toString();
        if (!snapshot.packageName.equals(currentPackage)) {
            return outcome("package_changed", "The foreground app changed after the snapshot.");
        }
        AccessibilityNodeInfo target = resolvePath(root, path);
        if (target == null) return outcome("target_stale", "The requested element moved after the snapshot.");

        if ("activate".equals(type)) {
            AccessibilityNodeInfo clickable = target;
            while (clickable != null && !clickable.isClickable()) clickable = clickable.getParent();
            return clickable != null && clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    ? outcome("ok", "Element activated.")
                    : outcome("target_stale", "The element could not be activated.");
        }
        if ("set_text".equals(type)) {
            Bundle arguments = new Bundle();
            arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                    action.optString("text", ""));
            return target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
                    ? outcome("ok", "Text updated.")
                    : outcome("target_stale", "The element no longer accepts text.");
        }
        if ("scroll".equals(type)) {
            int scrollAction = "backward".equals(action.optString("direction", ""))
                    ? AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                    : AccessibilityNodeInfo.ACTION_SCROLL_FORWARD;
            return target.performAction(scrollAction)
                    ? outcome("ok", "Element scrolled.")
                    : outcome("target_stale", "The element could not scroll.");
        }
        return outcome("unsupported_action", "Unsupported accessibility action: " + type);
    }

    private static JSONObject semanticNode(AccessibilityNodeInfo node, String ref, String parentRef) throws Exception {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        JSONArray actions = new JSONArray();
        if (node.isClickable()) actions.put("activate");
        if (node.isEditable()) actions.put("set_text");
        if (node.isScrollable()) actions.put("scroll_forward").put("scroll_backward");
        CharSequence text = node.getText();
        CharSequence description = node.getContentDescription();
        CharSequence role = node.getClassName();
        return new JSONObject()
                .put("ref", ref)
                .put("parentRef", parentRef == null ? JSONObject.NULL : parentRef)
                .put("role", role == null ? "" : role.toString())
                .put("text", text == null ? "" : text.toString())
                .put("contentDescription", description == null ? "" : description.toString())
                .put("viewId", node.getViewIdResourceName() == null ? "" : node.getViewIdResourceName())
                .put("bounds", new JSONArray().put(bounds.left).put(bounds.top).put(bounds.right).put(bounds.bottom))
                .put("flags", new JSONObject()
                        .put("clickable", node.isClickable())
                        .put("editable", node.isEditable())
                        .put("scrollable", node.isScrollable())
                        .put("enabled", node.isEnabled())
                        .put("focused", node.isFocused()))
                .put("actions", actions);
    }

    private static boolean isObservable(AccessibilityNodeInfo node) {
        CharSequence text = node.getText();
        CharSequence description = node.getContentDescription();
        return (text != null && text.length() > 0)
                || (description != null && description.length() > 0)
                || node.isClickable() || node.isEditable() || node.isScrollable();
    }

    private static AccessibilityNodeInfo resolvePath(AccessibilityNodeInfo root, String path) {
        AccessibilityNodeInfo current = root;
        if (path.isEmpty()) return current;
        for (String part : path.split("/")) {
            int index;
            try { index = Integer.parseInt(part); }
            catch (NumberFormatException error) { return null; }
            if (current == null || index < 0 || index >= current.getChildCount()) return null;
            current = current.getChild(index);
        }
        return current;
    }

    private static JSONObject global(PhoneAccessibilityService service, String name) throws Exception {
        int value;
        switch (name) {
            case "back": value = GLOBAL_ACTION_BACK; break;
            case "home": value = GLOBAL_ACTION_HOME; break;
            case "recents": value = GLOBAL_ACTION_RECENTS; break;
            case "notifications": value = GLOBAL_ACTION_NOTIFICATIONS; break;
            default: return outcome("unsupported_action", "Unsupported global action: " + name);
        }
        return service.performGlobalAction(value)
                ? outcome("ok", "Global action completed.")
                : outcome("target_stale", "Android rejected the global action.");
    }

    private static JSONObject gesture(PhoneAccessibilityService service, double x1, double y1,
                                      double x2, double y2, long durationMs) throws Exception {
        if (x1 < 0 || y1 < 0 || x2 < 0 || y2 < 0) {
            return outcome("target_not_found", "Gesture coordinates are required.");
        }
        Path path = new Path();
        path.moveTo((float) x1, (float) y1);
        path.lineTo((float) x2, (float) y2);
        GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, Math.max(1, durationMs))).build();
        return service.dispatchGesture(gesture, null, null)
                ? outcome("ok", "Gesture dispatched.")
                : outcome("target_stale", "Android rejected the gesture.");
    }

    private static JSONObject outcome(String code, String message) throws Exception {
        return new JSONObject().put("code", code).put("message", message);
    }

    private static JSONObject performLegacy(JSONObject params) throws Exception {
        PhoneAccessibilityService service = requireService();
        String action = params.optString("action", "");
        if ("back".equals(action) || "home".equals(action) || "recents".equals(action)
                || "notifications".equals(action)) return global(service, action);
        if ("tap".equals(action)) {
            return gesture(service, params.optDouble("x", -1), params.optDouble("y", -1),
                    params.optDouble("x", -1), params.optDouble("y", -1), 80);
        }
        return outcome("unsupported_action", "Legacy element actions require a semantic snapshot.");
    }

    private static PhoneAccessibilityService requireService() {
        PhoneAccessibilityService service = instance;
        if (service == null) throw new IllegalStateException("Accessibility Control is not enabled or connected.");
        return service;
    }

    private static final class PendingNode {
        final AccessibilityNodeInfo node;
        final String path;
        final String parentRef;

        PendingNode(AccessibilityNodeInfo node, String path, String parentRef) {
            this.node = node;
            this.path = path;
            this.parentRef = parentRef;
        }
    }

    private static final class SnapshotState {
        final String snapshotId;
        final String packageName;
        final Map<String, String> pathsByRef;

        SnapshotState(String snapshotId, String packageName, Map<String, String> pathsByRef) {
            this.snapshotId = snapshotId;
            this.packageName = packageName;
            this.pathsByRef = pathsByRef;
        }
    }
}
