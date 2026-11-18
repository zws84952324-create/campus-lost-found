// 所有主要功能在关闭 JavaScript 时仍可通过普通表单使用。
const imageInput = document.querySelector('[data-image-input]');
let previewUrl = null;
if (imageInput) {
  imageInput.addEventListener('change', () => {
    const box = document.querySelector('.image-preview');
    const preview = box.querySelector('img');
    const message = box.querySelector('[data-image-message]');
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    imageInput.setCustomValidity('');
    const file = imageInput.files[0];
    box.hidden = !file;
    if (!file) return;
    preview.hidden = false;
    if (file.size > 6 * 1024 * 1024) {
      imageInput.setCustomValidity('图片超过 6 MB，请换一张较小的照片。');
      message.textContent = '图片超过 6 MB，请换一张较小的照片。';
      preview.hidden = true;
      return;
    }
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    message.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
  });
}

const password = document.querySelector('input[autocomplete="new-password"][name="password"]');
const confirmation = document.querySelector('input[name="confirm_password"]');
if (password && confirmation) {
  const validate = () => confirmation.setCustomValidity(confirmation.value && confirmation.value !== password.value ? '两次输入的密码不一致。' : '');
  password.addEventListener('input', validate);
  confirmation.addEventListener('input', validate);
}

document.querySelectorAll('[data-confirm]').forEach(button => {
  button.addEventListener('click', event => {
    if (button.dataset.confirmed === 'yes' || !button.form.reportValidity()) return;
    event.preventDefault();
    const dialog = document.createElement('dialog');
    dialog.className = 'confirm-dialog';
    const title = document.createElement('h2');
    title.className = 'heading-5';
    title.id = 'confirmation-title';
    title.textContent = '确认这次操作';
    dialog.setAttribute('aria-labelledby', title.id);
    const description = document.createElement('p');
    description.className = 'body-3';
    description.textContent = button.dataset.confirm;
    const actions = document.createElement('div');
    actions.className = 'button-row';
    const cancel = document.createElement('button');
    cancel.className = 'button button-secondary button-sm';
    cancel.textContent = '再核对一下';
    const confirm = document.createElement('button');
    confirm.className = 'button button-sm';
    confirm.textContent = '确认操作';
    actions.append(cancel, confirm);
    dialog.append(title, description, actions);
    document.body.append(dialog);
    dialog.addEventListener('close', () => { dialog.remove(); button.focus(); });
    cancel.addEventListener('click', () => dialog.close());
    confirm.addEventListener('click', () => {
      button.dataset.confirmed = 'yes';
      dialog.close();
      button.form.requestSubmit(button);
    });
    dialog.showModal();
    cancel.focus();
  });
});

document.querySelectorAll('form[method="post"]').forEach(form => {
  form.addEventListener('submit', event => {
    if (form.dataset.submitting === 'yes') { event.preventDefault(); return; }
    form.dataset.submitting = 'yes';
    // 不禁用命名按钮，以免浏览器丢失审核/认领的 action 字段。
    if (event.submitter) {
      event.submitter.setAttribute('aria-busy', 'true');
      event.submitter.dataset.originalText = event.submitter.textContent;
      event.submitter.textContent = '正在处理…';
    }
  });
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('form').forEach(form => delete form.dataset.submitting);
  document.querySelectorAll('[aria-busy="true"]').forEach(button => {
    button.textContent = button.dataset.originalText;
    button.removeAttribute('aria-busy');
  });
});
