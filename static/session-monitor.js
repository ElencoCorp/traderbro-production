async function checkSession() {

    // Don't run on public pages or right after a deliberate logout
    if (sessionStorage.getItem("loggedOut")) return;

    try {

        const response = await fetch("/api/me");

        const user = await response.json();

        const currentPath = window.location.pathname;

        const excludedPages = [
            "/",
            "/user-login",
            "/register",
            "/admin-login"
        ];

        if (
            !user.username &&
            !excludedPages.some(p => currentPath === p || currentPath.startsWith(p + "?"))
        ) {

            alert(
                "Your account has been logged in on another device."
            );

            window.location.href = "/user-login";
        }

    } catch (e) {
        console.log("Session monitor:", e);
    }
}

setInterval(checkSession, 2000);
