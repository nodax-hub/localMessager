#include <QObject>
#include "ServicePublisher.h"
#include "DeviceDiscovery.h"
#include "WebSocketServer.h"
#include "WebSocketClient.h"

class Coordinator : public QObject {
    Q_OBJECT
public:
    explicit Coordinator(QObject* parent = nullptr)
        : QObject(parent),
          publisher(new ServicePublisher(this)),
          discovery(new DeviceDiscovery(this)),
          server(new WebSocketServer(this)),
          client(new WebSocketClient(this)) {

        connect(discovery, &DeviceDiscovery::serviceDiscovered, this, &Coordinator::onServiceDiscovered);
        connect(discovery, &DeviceDiscovery::serviceLost, this, &Coordinator::onServiceLost);
    }

private slots:
    void onServiceDiscovered(const QString& name, const QString& ip, int port) {
        qDebug() << "Connecting to discovered service:" << name;
        client->connectToServer(ip, port);
    }

    void onServiceLost(const QString& name) {
        qDebug() << "Service lost:" << name;
    }

private:
    ServicePublisher* publisher;
    DeviceDiscovery* discovery;
    WebSocketServer* server;
    WebSocketClient* client;
};
