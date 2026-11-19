
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
    const feeUploadForm = document.querySelector("[data-fee-upload-form]");
    const feeFileInput = document.querySelector("[data-fee-file]");
    const feePreview = document.querySelector("[data-fee-preview]");
    const feePreviewImage = document.querySelector("[data-fee-preview-image]");
    const feePreviewPdf = document.querySelector("[data-fee-preview-pdf]");
    const correctionIdInput = document.querySelector("[data-correction-id]");
    const correctionLabel = document.querySelector("[data-correction-label]");
    const cancelCorrectionButton = document.querySelector(
        "[data-cancel-correction]"
    );
    const bedAllocationForm = document.querySelector("[data-bed-allocation-form]");
    const passwordToggle = document.querySelector("[data-password-toggle]");
    const passwordField = document.querySelector("#password");
    let feePreviewUrl = null;

    messages.forEach(function (message) {
        setTimeout(function () {
            message.style.transition = "opacity 0.5s";
            message.style.opacity = "0";

            setTimeout(function () {
                message.remove();
            }, 500);

        }, 4000);

    });

    if (passwordToggle && passwordField) {
        passwordToggle.addEventListener("click", function () {
            const showPassword = passwordField.type === "password";
            passwordField.type = showPassword ? "text" : "password";
            passwordToggle.textContent = showPassword ? "Hide" : "Show";
            passwordToggle.setAttribute("aria-label", showPassword
                ? "Hide password" : "Show password");
            passwordToggle.setAttribute("aria-pressed", String(showPassword));
        });
    }

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

    function clearFeePreview() {
        if (feePreviewUrl) {
            URL.revokeObjectURL(feePreviewUrl);
            feePreviewUrl = null;
        }
        feePreviewImage.removeAttribute("src");
        feePreviewPdf.removeAttribute("src");
        feePreviewImage.hidden = true;
        feePreviewPdf.hidden = true;
        feePreview.hidden = true;
    }

    function resetFeeCorrection() {
        correctionIdInput.value = "";
        correctionLabel.textContent = "";
        correctionLabel.hidden = true;
        cancelCorrectionButton.hidden = true;
        feeUploadForm.reset();
        clearFeePreview();
    }

    if (feeUploadForm && feeFileInput) {
        feeFileInput.addEventListener("change", function () {
            clearFeePreview();
            const file = feeFileInput.files[0];
            if (!file) {
                return;
            }

            feePreviewUrl = URL.createObjectURL(file);
            feePreview.hidden = false;
            if (file.type === "application/pdf") {
                feePreviewPdf.src = feePreviewUrl;
                feePreviewPdf.hidden = false;
            } else if (file.type === "image/jpeg" || file.type === "image/png") {
                feePreviewImage.src = feePreviewUrl;
                feePreviewImage.hidden = false;
            } else {
                clearFeePreview();
            }
        });

        document.querySelectorAll("[data-fee-correction]").forEach(
            function (button) {
                button.addEventListener("click", function () {
                    correctionIdInput.value = button.dataset.feeCorrection;
                    feeUploadForm.elements.amount.value = button.dataset.amount;
                    feeUploadForm.elements.payment_date.value =
                        button.dataset.paymentDate;
                    feeUploadForm.elements.transaction_id.value =
                        button.dataset.transactionId;
                    feeUploadForm.elements.academic_year.value =
                        button.dataset.academicYear;
                    feeFileInput.value = "";
                    correctionLabel.textContent =
                        "Correcting rejected receipt #" + button.dataset.feeCorrection;
                    correctionLabel.hidden = false;
                    cancelCorrectionButton.hidden = false;
                    clearFeePreview();
                    feeUploadForm.scrollIntoView({ behavior: "smooth" });
                    feeFileInput.focus();
                });
            }
        );

        cancelCorrectionButton.addEventListener("click", resetFeeCorrection);
    }

    if (bedAllocationForm) {
        const buildingSelect = bedAllocationForm.querySelector(
            "[data-building-select]"
        );
        const floorSelect = bedAllocationForm.querySelector("[data-floor-select]");
        const roomSelect = bedAllocationForm.querySelector("[data-room-select]");
        const bedSelect = bedAllocationForm.querySelector("[data-bed-select]");

        function filterOptions(select, predicate) {
            Array.from(select.options).forEach(function (option) {
                option.hidden = option.value !== "" && !predicate(option);
            });
            if (select.selectedOptions[0] && select.selectedOptions[0].hidden) {
                select.value = "";
            }
        }

        function updateFloors() {
            filterOptions(floorSelect, function (option) {
                return option.dataset.buildingId === buildingSelect.value;
            });
            updateRooms();
        }

        function updateRooms() {
            filterOptions(roomSelect, function (option) {
                return option.dataset.buildingId === buildingSelect.value &&
                    option.dataset.floorId === floorSelect.value;
            });
            updateBeds();
        }

        function updateBeds() {
            filterOptions(bedSelect, function (option) {
                return option.dataset.roomId === roomSelect.value;
            });
        }

        buildingSelect.addEventListener("change", updateFloors);
        floorSelect.addEventListener("change", updateRooms);
        roomSelect.addEventListener("change", updateBeds);
        updateFloors();
    }

});
