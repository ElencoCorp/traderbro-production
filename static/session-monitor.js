async function checkSession() {

    try {

        const response = await fetch("/api/me");

        const user = await response.json();

        const currentPath = window.location.pathname;

        const excludedPages = [
            "/user-login",
            "/register",
            "/admin-login"
        ];

        if (
            !user.username &&
            !excludedPages.some(p => currentPath.includes(p))
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