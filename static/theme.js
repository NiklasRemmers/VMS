/**
 * Consistent Theme Handling (Dark/Light Mode)
 * - Applies theme immediately to prevent FOUC
 * - Handles toggle button clicks
 * - Syncs across tabs via localStorage
 */
(function () {
    // 1. Initial Theme Application
    // Run immediately to prevent Flash of Incorrect Theme
    const html = document.documentElement;
    const savedTheme = localStorage.getItem('theme');
    const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;

    if (savedTheme === 'dark' || (!savedTheme && systemDark)) {
        html.classList.add('dark');
    } else {
        html.classList.remove('dark');
    }

    // 2. Setup Toggle on DOMContentLoaded
    document.addEventListener('DOMContentLoaded', () => {
        const themeToggle = document.getElementById('themeToggle');
        const sunIcon = document.getElementById('sunIcon');
        const moonIcon = document.getElementById('moonIcon');

        // Function to update icons
        function updateIcons() {
            // Check current state from classList
            const isDark = html.classList.contains('dark');

            // Only update if icons exist
            if (sunIcon && moonIcon) {
                if (isDark) {
                    // Dark mode: Show Sun (to switch to light)
                    sunIcon.classList.remove('hidden');
                    moonIcon.classList.add('hidden');
                } else {
                    // Light mode: Show Moon (to switch to dark)
                    sunIcon.classList.add('hidden');
                    moonIcon.classList.remove('hidden');
                }
            }
        }

        // Initialize icons
        updateIcons();

        // Add Click Listener
        if (themeToggle) {
            themeToggle.addEventListener('click', () => {
                // Toggle class
                if (html.classList.contains('dark')) {
                    html.classList.remove('dark');
                    localStorage.setItem('theme', 'light');
                } else {
                    html.classList.add('dark');
                    localStorage.setItem('theme', 'dark');
                }
                // Update icons
                updateIcons();
            });
        }
    });
})();
