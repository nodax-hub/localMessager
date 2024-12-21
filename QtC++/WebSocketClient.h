#include <QObject>
#include <QWebSocket>
#include <QDebug>

class WebSocketClient : public QObject {
    Q_OBJECT
public:
    explicit WebSocketClient(QObject* parent = nullptr) : QObject(parent), socket(new QWebSocket) {
        connect(socket, &QWebSocket::connected, this, &WebSocketClient::onConnected);
        connect(socket, &QWebSocket::disconnected, this, &WebSocketClient::onDisconnected);
        connect(socket, &QWebSocket::textMessageReceived, this, &WebSocketClient::onMessageReceived);
    }

    void connectToServer(const QString& address, int port) {
        QUrl url(QString("ws://%1:%2").arg(address).arg(port));
        socket->open(url);
    }

private slots:
    void onConnected() {
        qDebug() << "Connected to server";
        socket->sendTextMessage("Hello, server!");
    }

    void onDisconnected() {
        qDebug() << "Disconnected from server";
    }

    void onMessageReceived(const QString& message) {
        qDebug() << "Message from server:" << message;
    }

private:
    QWebSocket* socket;
};
