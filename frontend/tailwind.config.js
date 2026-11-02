/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // 微众银行风格蓝
        webank: {
          50: '#eef3ff',
          100: '#d9e3ff',
          200: '#b6c8ff',
          300: '#8aa6ff',
          400: '#5a7dfb',
          500: '#2b5aed',
          600: '#1f47d6',
          700: '#1a39ad',
          800: '#18318a',
          900: '#162c6e',
        },
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
      },
      boxShadow: {
        card: '0 2px 12px rgba(27, 57, 173, 0.08)',
      },
    },
  },
  plugins: [],
}
