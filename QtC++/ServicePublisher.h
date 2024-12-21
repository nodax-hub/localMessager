#include <QObject>
#include <QZeroConf>
#include <QZeroConfService>
#include <QDebug>

class ServicePublisher : public QObject {
    Q_OBJECT
public:
    explicit ServicePublisher(QObject* parent = nullptr) : QObject(parent) {
        publishService();
    }

private:
    void publishService() {
        QZeroConf zeroConf;
        QZeroConfService service;
        service.setName("MyQtService");
        service.setType("_myservice._tcp");
        service.setPort(12345);

        if (!zeroConf.startServicePublish(service)) {
            qCritical() << "Failed to publish service";
        } else {
            qDebug() << "Service published successfully";
        }
    }
};
