/* iStealClips — Global JS Utilities */

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
}

function formatTimeAgo(mtimeSeconds) {
    if (!mtimeSeconds) return '';
    const now = Math.floor(Date.now() / 1000);
    const diff = Math.max(0, now - mtimeSeconds);

    if (diff < 60) return 'Just now';
    if (diff < 3600) {
        const mins = Math.floor(diff / 60);
        return `${mins} min${mins > 1 ? 's' : ''} ago`;
    }
    if (diff < 86400) {
        const hours = Math.floor(diff / 3600);
        return `${hours} hr${hours > 1 ? 's' : ''} ago`;
    }
    const days = Math.floor(diff / 86400);
    return `${days} day${days > 1 ? 's' : ''} ago`;
}

// Highlight active nav link
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach(link => {
        const href = link.getAttribute('href');
        if (href === path || (href !== '/' && path.startsWith(href))) {
            link.classList.add('text-violet-400', 'font-semibold');
            link.classList.remove('text-gray-400');
        }
    });
});
