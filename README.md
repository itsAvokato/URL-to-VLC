# URL to VLC
Небольшая утилита для Windows, которая висит в трее и помогает автоматически открывать ссылки вида http(s)://host[:port]/stream/... в VLC.

## Возможности
* Мониторинг буфера обмена через WinAPI: реагирует на копирование ссылок /stream/ и /ts/stream/.
* Список последних ссылок в меню трея (до 5), с возможностью открыть или удалить.
* Пункт меню «Открыть из буфера» — открыть текущую ссылку вручную.
* Переключатель Вкл/Выкл.
* Автозапуск через меню (Windows Registry, HKCU\Run).

История хранится рядом с .exe в файле lampa_urls.json.

## Требования
Windows 10/11  
[VLC](https://www.videolan.org/vlc/)

## Сборка   
```pip install pyinstaller pyperclip pystray pillow plyer```  
```pyinstaller --onefile --noconsole --name "URL to VLC" --icon eye.ico "url to vlc.py"```  

---
https://github.com/user-attachments/assets/8fff7917-8550-4d95-b2cd-e17dac0719e0

