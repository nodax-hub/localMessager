#include <QObject>
#include <QWebSocketServer>
#include <QWebSocket>
#include <QDebug>

class WebSocketServer : public QObject {
    Q_OBJECT
public:
    explicit WebSocketServer(QObject* parent = nullptr)
        : QObject(parent), server(new QWebSocketServer("WebSocketServer", QWebSocketServer::NonSecureMode, this)) {
        if (server->listen(QHostAddress::Any, 12345)) {
            qDebug() << "WebSocket server started on port 12345";
            connect(server, &QWebSocketServer::newConnection, this, &WebSocketServer::onNewConnection);
        } else {
            qCritical() << "Failed to start WebSocket server";
        }
    }

private slots:
    void onNewConnection() {
        QWebSocket* client = server->nextPendingConnection();
        qDebug() << "New client connected:" << client->peerAddress().toString();
        clients.append(client);

        connect(client, &QWebSocket::disconnected, this, [this, client]() {
            qDebug() << "Client disconnected:" << client->peerAddress().toString();
            clients.removeAll(client);
            client->deleteLater();
        });

        connect(client, &QWebSocket::textMessageReceived, this, [this, client](const QString& message) {
            qDebug() << "Message received:" << message;
            client->sendTextMessage("Echo: " + message);
        });
    }

private:
    QWebSocketServer* server;
    QList<QWebSocket*> clients;
};
