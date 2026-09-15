const ham = document.querySelector('.ham')
const hamIcon = ham.querySelector('span')
const mobileMenu = document.querySelector('.mobile-menu')

ham.addEventListener('click', () => {
    hamIcon.innerText = ham.innerText === 'menu' ? 'close' : 'menu'
    mobileMenu.classList.toggle('is-open')
} )
