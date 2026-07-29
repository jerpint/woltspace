export async function requestAndSendDemo() {
  if (!window.__TAURI_INTERNALS__) {
    if (!("Notification" in window)) return "unsupported";
    const permission = await Notification.requestPermission();
    if (permission === "granted") new Notification("Woltspace", { body: "Native alerts are ready for lodge activity." });
    return permission;
  }

  const { isPermissionGranted, requestPermission, sendNotification } = await import("@tauri-apps/plugin-notification");
  let granted = await isPermissionGranted();
  if (!granted) granted = (await requestPermission()) === "granted";
  if (granted) sendNotification({ title: "Woltspace", body: "Native alerts are ready for lodge activity." });
  return granted ? "granted" : "denied";
}
