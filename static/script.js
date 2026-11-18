
// Display confirmation before deleting a student
function confirmDelete() {
    return confirm("Are you sure you want to delete this student?");
}

// Automatically hide success and error messages
document.addEventListener("DOMContentLoaded", function () {

    const messages = document.querySelectorAll(".message");
    const attendanceSearch = document.querySelector("[data-attendance-search]");
    const attendanceRows = document.querySelectorAll("[data-attendance-row]");
    const markAllPresentButton = document.querySelector(
        "[data-attendance-mark-all]"
    );

    messages.forEach(function (message) {
        setTimeout(function () {
            message.style.transition = "opacity 0.5s";
            message.style.opacity = "0";

            setTimeout(function () {
                message.remove();
            }, 500);

        }, 4000);

    });

    if (attendanceSearch) {
        attendanceSearch.addEventListener("input", function () {
            const query = attendanceSearch.value.trim().toLowerCase();
            attendanceRows.forEach(function (row) {
                row.hidden = !row.dataset.search.includes(query);
            });
        });
    }

    if (markAllPresentButton) {
        markAllPresentButton.addEventListener("click", function () {
            document.querySelectorAll(
                'input[type="radio"][data-attendance-status][value="Present"]'
            ).forEach(
                function (statusField) {
                    statusField.checked = true;
                }
            );
        });
    }

});
